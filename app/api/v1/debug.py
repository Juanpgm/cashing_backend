"""Debug / control panel — development only.

Exposes introspection endpoints and a SPA dashboard for the CashIn agent backend.
All routes are guarded by _is_dev() and return 403 in production.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import pkgutil
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.credito import Credito, TipoCredito
from app.models.usuario import Usuario

logger = structlog.get_logger("api.debug")

router = APIRouter(prefix="/debug", tags=["debug"])

# ---------------------------------------------------------------------------
# Log capture
# ---------------------------------------------------------------------------

_LOG_BUFFER: deque[dict[str, Any]] = deque(maxlen=500)
_LOG_QUEUES: list[asyncio.Queue[dict[str, Any]]] = []


class _DebugLogHandler(logging.Handler):
    """Captures all log records into the debug buffer and live SSE queues."""

    def emit(self, record: logging.LogRecord) -> None:
        entry: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
            "ts": int(record.created * 1000),
        }
        _LOG_BUFFER.append(entry)
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in _LOG_QUEUES:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            with contextlib.suppress(ValueError):
                _LOG_QUEUES.remove(q)


_debug_handler = _DebugLogHandler()
_debug_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_debug_handler)

# ---------------------------------------------------------------------------
# Secret field masking
# ---------------------------------------------------------------------------

SECRET_FIELDS = {
    "JWT_SECRET_KEY",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "WOMPI_PRIVATE_KEY",
    "WOMPI_EVENTS_SECRET",
    "TOKEN_ENCRYPTION_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
}

# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def _is_dev() -> None:
    """Raise 403 if not in a development environment."""
    env = settings.ENVIRONMENT.lower()
    if env not in ("development", "dev", "local", "test"):
        raise HTTPException(status_code=403, detail="Debug panel is disabled in production")


# ---------------------------------------------------------------------------
# Pydantic request/response schemas
# ---------------------------------------------------------------------------


class AgregarCreditosRequest(BaseModel):
    cantidad: int
    nota: str | None = None


class AgentInvokeRequest(BaseModel):
    message: str
    mode_override: str | None = None
    extra_state: dict[str, Any] | None = None


class AgentInvokeResponse(BaseModel):
    response: str
    mode: str
    error: str | None
    duration_ms: float
    state_keys: list[str]


class NodeInvokeRequest(BaseModel):
    node_name: str
    state: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _import_module_safe(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


def _safe_config() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field_name in settings.model_fields:
        value = getattr(settings, field_name, None)
        if field_name in SECRET_FIELDS and value:
            result[field_name] = "****"
        else:
            result[field_name] = value
    # Also include computed properties
    result["is_production"] = settings.is_production
    result["is_development"] = settings.is_development
    return result


def _scan_prompts() -> list[dict[str, Any]]:
    """Return all string-valued module-level variables from app.agent.prompts.*"""
    prompts_pkg = _import_module_safe("app.agent.prompts")
    if prompts_pkg is None:
        return []

    prompts_path = Path(prompts_pkg.__file__).parent  # type: ignore[arg-type]
    results: list[dict[str, Any]] = []

    for _finder, module_name, _ in pkgutil.iter_modules([str(prompts_path)]):
        full_name = f"app.agent.prompts.{module_name}"
        mod = _import_module_safe(full_name)
        if mod is None:
            continue
        for var_name in dir(mod):
            if var_name.startswith("_"):
                continue
            val = getattr(mod, var_name)
            if isinstance(val, str) and len(val) > 20:
                results.append(
                    {
                        "name": var_name,
                        "module": module_name,
                        "content": val,
                        "length": len(val),
                    }
                )
    return results


def _scan_tools() -> list[dict[str, Any]]:
    """Return metadata for every callable exported from app.agent.tools.*"""
    tools_pkg = _import_module_safe("app.agent.tools")
    if tools_pkg is None:
        return []

    tools_path = Path(tools_pkg.__file__).parent  # type: ignore[arg-type]
    results: list[dict[str, Any]] = []

    for _finder, module_name, _ in pkgutil.iter_modules([str(tools_path)]):
        full_name = f"app.agent.tools.{module_name}"
        mod = _import_module_safe(full_name)
        if mod is None:
            continue
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if callable(obj) and inspect.isfunction(obj):
                try:
                    sig = str(inspect.signature(obj))
                except (ValueError, TypeError):
                    sig = "(?)"
                results.append(
                    {
                        "name": attr_name,
                        "module": module_name,
                        "signature": f"{attr_name}{sig}",
                        "doc": (inspect.getdoc(obj) or "").split("\n")[0],
                        "is_async": inspect.iscoroutinefunction(obj),
                    }
                )
    return results


def _scan_nodes() -> list[dict[str, Any]]:
    """Return node names registered in the compiled graph."""
    try:
        from app.services.agent_service import get_graph

        graph = get_graph()
        nodes = list(getattr(graph, "nodes", {}).keys())
        return [{"name": n, "module": "agent.graph"} for n in nodes]
    except Exception as exc:
        return [{"name": "error", "module": str(exc)}]


def _graph_structure() -> dict[str, Any]:
    """Return edges and node list from the compiled graph."""
    try:
        from app.services.agent_service import get_graph

        graph = get_graph()
        nodes = list(getattr(graph, "nodes", {}).keys())
        # Build a simplified edge list by inspecting the underlying graph
        edges: list[dict[str, str]] = []
        # LangGraph compiled graph exposes .graph attribute with edge info
        inner = getattr(graph, "graph", None)
        if inner is not None:
            for edge in getattr(inner, "edges", set()):
                edges.append({"from": str(edge[0]), "to": str(edge[1])})
        mode_routing = {
            "chat": "chat",
            "pipeline": "doc_ingestion",
            "evidence": "email_fetch",
            "drive": "drive_upload",
            "extract_obligations": "extraction_router",
            "generate_activities": "generate_activities",
            "config": "chat",
        }
        return {"nodes": nodes, "edges": edges, "mode_routing": mode_routing}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def debug_panel_ui() -> HTMLResponse:
    """Serve the embedded SPA debug panel."""
    _is_dev()
    return HTMLResponse(content=_DEBUG_HTML)


@router.get("/info")
async def debug_info() -> dict[str, Any]:
    """Return environment, modes, and LLM configuration."""
    _is_dev()
    return {
        "environment": settings.ENVIRONMENT,
        "python_version": sys.version,
        "modes": [m.value for m in __import__("app.schemas.agent", fromlist=["AgentMode"]).AgentMode],
        "llm": {
            "default": settings.LLM_DEFAULT_MODEL,
            "fallback": settings.LLM_FALLBACK_MODEL,
            "extraction": settings.LLM_EXTRACTION_MODEL,
            "local": settings.LLM_LOCAL_MODEL,
        },
    }


@router.get("/graph")
async def debug_graph() -> dict[str, Any]:
    """Return the compiled graph structure."""
    _is_dev()
    return _graph_structure()


@router.get("/prompts")
async def debug_prompts() -> list[dict[str, Any]]:
    """Return all agent prompts with content."""
    _is_dev()
    return _scan_prompts()


@router.get("/tools")
async def debug_tools() -> list[dict[str, Any]]:
    """Return all agent tool functions with signatures."""
    _is_dev()
    return _scan_tools()


@router.get("/nodes")
async def debug_nodes() -> list[dict[str, Any]]:
    """Return all nodes registered in the compiled graph."""
    _is_dev()
    return _scan_nodes()


@router.get("/config")
async def debug_config() -> dict[str, Any]:
    """Return safe application configuration (secrets masked)."""
    _is_dev()
    return _safe_config()


@router.post("/agent/invoke", response_model=AgentInvokeResponse)
async def debug_agent_invoke(req: AgentInvokeRequest) -> AgentInvokeResponse:
    """Invoke the agent graph with a test message (no auth required)."""
    _is_dev()
    try:
        from app.schemas.agent import AgentMode
        from app.services.agent_service import get_graph

        graph = get_graph()

        dummy_user = uuid.uuid4()
        dummy_session = uuid.uuid4()

        state: dict[str, Any] = {
            "session_id": dummy_session,
            "user_id": dummy_user,
            "mode": AgentMode.CHAT,
            "messages": [],
            "user_input": req.message,
            "response": "",
        }

        if req.mode_override:
            with contextlib.suppress(ValueError):
                state["mode"] = AgentMode(req.mode_override)

        if req.extra_state:
            state.update(req.extra_state)

        t0 = time.monotonic()
        result = await graph.ainvoke(state)
        duration_ms = (time.monotonic() - t0) * 1000

        mode_val = result.get("mode", AgentMode.CHAT)
        mode_str = mode_val.value if hasattr(mode_val, "value") else str(mode_val)

        return AgentInvokeResponse(
            response=result.get("response", ""),
            mode=mode_str,
            error=result.get("error"),
            duration_ms=round(duration_ms, 2),
            state_keys=[k for k in result if not k.startswith("_")],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/node/invoke")
async def debug_node_invoke(req: NodeInvokeRequest) -> dict[str, Any]:
    """Invoke a single graph node directly with a supplied state dict."""
    _is_dev()
    # Map node names to their actual importable async functions
    NODE_FN_MAP: dict[str, str] = {
        "router": "app.agent.nodes.router:router_node",
        "chat": "app.agent.nodes.chat:chat_node",
        "doc_ingestion": "app.agent.nodes.pipeline:doc_ingestion_node",
        "doc_understanding": "app.agent.nodes.pipeline:doc_understanding_node",
        "classification": "app.agent.nodes.pipeline:classification_node",
        "justification": "app.agent.nodes.pipeline:justification_node",
        "email_fetch": "app.agent.nodes.email_fetch:email_fetch_node",
        "drive_upload": "app.agent.nodes.drive_upload:drive_upload_node",
        "contract_metadata": "app.agent.nodes.extraction:contract_metadata_node",
        "obligations_extraction": "app.agent.nodes.extraction:obligations_extraction_node",
        "generate_activities": "app.agent.nodes.activities:generate_activities_node",
    }
    try:
        if req.node_name not in NODE_FN_MAP:
            raise HTTPException(status_code=404, detail=f"Node '{req.node_name}' not found. Available: {list(NODE_FN_MAP)}")

        module_path, fn_name = NODE_FN_MAP[req.node_name].split(":")
        mod = importlib.import_module(module_path)
        node_fn = getattr(mod, fn_name)

        t0 = time.monotonic()
        if inspect.iscoroutinefunction(node_fn):
            result = await node_fn(req.state)
        else:
            result = node_fn(req.state)
        duration_ms = (time.monotonic() - t0) * 1000

        # Filter non-serializable values
        safe_result: dict[str, Any] = {}
        for k, v in (result or req.state).items():
            if k.startswith("_"):
                continue
            try:
                json.dumps(v, default=str)
                safe_result[k] = v
            except Exception:
                safe_result[k] = str(v)

        return {"node": req.node_name, "duration_ms": round(duration_ms, 2), "state": safe_result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/logs/history")
async def debug_logs_history() -> dict[str, Any]:
    """Return the last 200 log entries from the circular buffer."""
    _is_dev()
    entries = list(_LOG_BUFFER)[-200:]
    return {"count": len(entries), "entries": entries}


@router.get("/logs/stream")
async def debug_logs_stream() -> StreamingResponse:
    """SSE stream of live log entries."""
    _is_dev()

    async def event_generator() -> Any:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        _LOG_QUEUES.append(q)
        try:
            # Send buffered history first (last 50)
            for entry in list(_LOG_BUFFER)[-50:]:
                yield f"data: {json.dumps(entry)}\n\n"
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(entry)}\n\n"
                except TimeoutError:
                    # Keepalive comment
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with contextlib.suppress(ValueError):
                _LOG_QUEUES.remove(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/creditos/balance")
async def debug_creditos_balance(
    user: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return real credit balance for the authenticated user (dev only)."""
    _is_dev()
    ingreso_result = await db.execute(
        select(func.coalesce(func.sum(Credito.cantidad), 0)).where(
            Credito.usuario_id == user.id,
            Credito.tipo.in_([TipoCredito.COMPRA, TipoCredito.BONUS]),
        )
    )
    consumo_result = await db.execute(
        select(func.coalesce(func.sum(Credito.cantidad), 0)).where(
            Credito.usuario_id == user.id,
            Credito.tipo == TipoCredito.CONSUMO,
        )
    )
    ingreso: int = int(ingreso_result.scalar_one() or 0)
    consumo: int = int(consumo_result.scalar_one() or 0)  # negative
    saldo: int = max(0, ingreso + consumo)
    return {
        "usuario_id": str(user.id),
        "email": user.email,
        "creditos_disponibles_cache": user.creditos_disponibles,
        "ingreso_total": ingreso,
        "consumido_total": abs(consumo),
        "saldo_real": saldo,
    }


@router.post("/creditos/agregar")
async def debug_agregar_creditos(
    req: AgregarCreditosRequest,
    user: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Add credits to the authenticated user for testing (dev only — never deploy to prod)."""
    _is_dev()
    if req.cantidad <= 0:
        raise HTTPException(status_code=400, detail="cantidad must be positive")
    credito = Credito(
        usuario_id=user.id,
        cantidad=req.cantidad,
        tipo=TipoCredito.BONUS,
        referencia=f"debug_add:{req.nota or 'test'}",
    )
    db.add(credito)
    user.creditos_disponibles += req.cantidad
    await db.commit()
    logger.info("debug_creditos_agregados", usuario_id=str(user.id), cantidad=req.cantidad)
    return {
        "added": req.cantidad,
        "creditos_disponibles_cache": user.creditos_disponibles,
        "message": f"Added {req.cantidad} credits to {user.email}",
    }


@router.get("/mcp/config")
async def debug_mcp_config() -> dict[str, Any]:
    """Return MCP server configuration and file list."""
    _is_dev()
    root = _get_project_root()

    # Read .claude/settings.json
    settings_path = root / ".claude" / "settings.json"
    mcp_servers: dict[str, Any] = {}
    if settings_path.exists():
        try:
            with settings_path.open() as f:
                data = json.load(f)
            mcp_servers = data.get("mcpServers", {})
        except Exception as exc:
            mcp_servers = {"error": str(exc)}

    # Also check settings.local.json
    local_settings_path = root / ".claude" / "settings.local.json"
    local_mcp: dict[str, Any] = {}
    if local_settings_path.exists():
        with contextlib.suppress(Exception):
            with local_settings_path.open() as f:
                local_data = json.load(f)
            local_mcp = local_data.get("mcpServers", {})

    # List mcp_servers directory
    mcp_dir = root / "mcp_servers"
    mcp_files: list[dict[str, Any]] = []
    if mcp_dir.exists():
        for f in sorted(mcp_dir.iterdir()):
            if f.suffix == ".py":
                server_name = f.stem
                cmd_info = mcp_servers.get(server_name, local_mcp.get(server_name, {}))
                mcp_files.append(
                    {
                        "name": server_name,
                        "file": f.name,
                        "exists": True,
                        "command": cmd_info.get("command", ""),
                        "args": cmd_info.get("args", []),
                    }
                )

    # Add configured servers that don't have files
    all_servers = {**mcp_servers, **local_mcp}
    for sname, sinfo in all_servers.items():
        if not any(f["name"] == sname for f in mcp_files):
            mcp_files.append(
                {
                    "name": sname,
                    "file": f"{sname}.py",
                    "exists": False,
                    "command": sinfo.get("command", ""),
                    "args": sinfo.get("args", []),
                }
            )

    return {
        "mcp_servers": all_servers,
        "files": mcp_files,
        "settings_path": str(settings_path),
        "local_settings_path": str(local_settings_path),
    }


# ---------------------------------------------------------------------------
# Embedded SPA HTML
# ---------------------------------------------------------------------------

_DEBUG_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CashIn Debug Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-primary: #09090b;
    --bg-secondary: #111113;
    --bg-tertiary: #18181b;
    --bg-hover: #1f1f24;
    --bg-active: #27272a;
    --accent: #6d28d9;
    --accent-light: #8b5cf6;
    --accent-dim: #2e1065;
    --accent-glow: rgba(109,40,217,0.15);
    --border: #27272a;
    --border-bright: #3f3f46;
    --text-primary: #fafafa;
    --text-secondary: #a1a1aa;
    --text-muted: #52525b;
    --green: #22c55e;
    --green-dim: #14532d;
    --yellow: #eab308;
    --yellow-dim: #713f12;
    --red: #ef4444;
    --red-dim: #450a0a;
    --blue: #3b82f6;
    --orange: #f97316;
    --radius: 10px;
    --radius-sm: 6px;
    --radius-xs: 4px;
    --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --font-mono: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.4);
    --shadow: 0 4px 12px rgba(0,0,0,0.5);
    --shadow-lg: 0 8px 24px rgba(0,0,0,0.6);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; background: var(--bg-primary); color: var(--text-primary); font-family: var(--font-sans); font-size: 14px; -webkit-font-smoothing: antialiased; }

  /* Layout */
  #app { display: flex; height: 100vh; overflow: hidden; }
  #sidebar {
    width: 240px; min-width: 240px;
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    box-shadow: var(--shadow);
  }
  #main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

  /* Sidebar brand */
  .sidebar-brand {
    padding: 20px 18px 14px;
    display: flex; align-items: center; gap: 10px;
    border-bottom: 1px solid var(--border);
  }
  .sidebar-brand-icon {
    width: 32px; height: 32px; border-radius: 8px;
    background: linear-gradient(135deg, var(--accent), #a855f7);
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; box-shadow: 0 2px 8px rgba(109,40,217,0.4);
  }
  .sidebar-brand-text { font-weight: 700; font-size: 15px; letter-spacing: -0.3px; }
  .sidebar-brand-text small { display: block; font-size: 10px; font-weight: 500; color: var(--text-muted); letter-spacing: 0.5px; text-transform: uppercase; }

  .sidebar-section { padding: 12px 10px 4px; }
  .sidebar-section-label { font-size: 10px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.2px; padding: 0 8px 6px; }

  .tab-btn {
    display: flex; align-items: center; gap: 9px;
    width: 100%; padding: 8px 10px; border: none;
    background: none; color: var(--text-secondary);
    cursor: pointer; font-size: 13px; font-weight: 500;
    text-align: left; transition: all 0.12s;
    border-radius: var(--radius-sm); margin-bottom: 1px;
  }
  .tab-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
  .tab-btn.active {
    background: var(--accent-glow);
    color: var(--accent-light);
    font-weight: 600;
    box-shadow: inset 0 0 0 1px rgba(109,40,217,0.2);
  }
  .tab-icon { font-size: 14px; width: 18px; text-align: center; }

  /* Header */
  #header {
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    height: 52px;
    display: flex; align-items: center; gap: 12px;
    box-shadow: var(--shadow-sm);
  }
  #header .breadcrumb { font-size: 13px; color: var(--text-muted); display: flex; align-items: center; gap: 6px; }
  #header .breadcrumb .current { color: var(--text-primary); font-weight: 600; }
  #header .env-badge {
    background: linear-gradient(135deg, rgba(109,40,217,0.3), rgba(168,85,247,0.2));
    color: var(--accent-light); font-size: 10px; padding: 3px 10px;
    border-radius: 20px; font-weight: 700; letter-spacing: 0.5px;
    border: 1px solid rgba(109,40,217,0.3); text-transform: uppercase;
  }
  #header .spacer { flex: 1; }
  #header .user-info { font-size: 12px; color: var(--text-secondary); }
  #header .btn-login {
    background: var(--accent); color: #fff; border: none;
    padding: 6px 14px; border-radius: var(--radius-sm);
    cursor: pointer; font-size: 12px; font-weight: 600;
    transition: all 0.12s; letter-spacing: 0.2px;
  }
  #header .btn-login:hover { background: var(--accent-light); box-shadow: 0 0 12px rgba(109,40,217,0.4); }

  /* Content area */
  #content { flex: 1; overflow-y: auto; padding: 28px 32px; }

  /* Cards */
  .card {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 22px 24px;
    margin-bottom: 16px;
    box-shadow: var(--shadow-sm);
  }
  .card-title {
    font-size: 11px; font-weight: 700; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 18px;
    display: flex; align-items: center; gap: 8px;
  }
  .card-title::before {
    content: ''; display: block; width: 3px; height: 14px;
    background: linear-gradient(180deg, var(--accent), #a855f7);
    border-radius: 2px;
  }

  /* Forms */
  label { display: block; font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; font-weight: 500; letter-spacing: 0.1px; }
  input[type="text"], input[type="password"], input[type="email"], textarea, select {
    width: 100%; background: var(--bg-primary);
    border: 1px solid var(--border); color: var(--text-primary);
    padding: 9px 12px; border-radius: var(--radius-sm); font-size: 13px;
    outline: none; transition: border 0.12s, box-shadow 0.12s;
    font-family: var(--font-sans);
  }
  input:focus, textarea:focus, select:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(109,40,217,0.15);
  }
  textarea { font-family: var(--font-mono); resize: vertical; min-height: 80px; line-height: 1.5; }
  select option { background: var(--bg-tertiary); }
  .form-row { margin-bottom: 16px; }

  .btn {
    padding: 9px 18px; border: none; border-radius: var(--radius-sm);
    cursor: pointer; font-size: 13px; font-weight: 600;
    transition: all 0.12s; letter-spacing: 0.2px; font-family: var(--font-sans);
  }
  .btn-primary {
    background: linear-gradient(135deg, var(--accent), #7c3aed);
    color: #fff; box-shadow: 0 2px 8px rgba(109,40,217,0.3);
  }
  .btn-primary:hover { background: linear-gradient(135deg, var(--accent-light), var(--accent)); box-shadow: 0 4px 14px rgba(109,40,217,0.4); transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; transform: none; box-shadow: none; }
  .btn-secondary { background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border-bright); }
  .btn-secondary:hover { background: var(--bg-active); border-color: var(--text-muted); }
  .btn-danger { background: var(--red-dim); color: #fca5a5; border: 1px solid rgba(239,68,68,0.3); }
  .btn-danger:hover { background: #7f1d1d; }
  .btn-sm { padding: 5px 12px; font-size: 11px; }

  /* Response bubbles */
  .response-bubble {
    background: var(--bg-primary); border: 1px solid var(--border-bright);
    border-radius: var(--radius); padding: 16px 18px; font-size: 13.5px;
    line-height: 1.7; white-space: pre-wrap; word-break: break-word;
    font-family: var(--font-sans); color: var(--text-primary);
  }
  .response-meta { display: flex; gap: 16px; margin-top: 12px; flex-wrap: wrap; }
  .meta-item { font-size: 12px; color: var(--text-secondary); background: var(--bg-tertiary); padding: 3px 10px; border-radius: 20px; }
  .meta-item span { color: var(--accent-light); font-weight: 600; }

  /* Badges */
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 700; letter-spacing: 0.3px; text-transform: uppercase; }
  .badge-async { background: rgba(20,78,74,0.5); color: #5eead4; border: 1px solid rgba(94,234,212,0.2); }
  .badge-sync { background: rgba(30,27,75,0.5); color: #a5b4fc; border: 1px solid rgba(165,180,252,0.2); }
  .badge-green { background: var(--green-dim); color: #86efac; border: 1px solid rgba(134,239,172,0.2); }
  .badge-red { background: var(--red-dim); color: #fca5a5; border: 1px solid rgba(252,165,165,0.2); }
  .badge-yellow { background: var(--yellow-dim); color: #fde68a; border: 1px solid rgba(253,230,138,0.2); }
  .badge-purple { background: var(--accent-dim); color: var(--accent-light); border: 1px solid rgba(109,40,217,0.3); }

  /* Code / pre */
  pre {
    font-family: var(--font-mono); font-size: 12px;
    background: var(--bg-primary); border: 1px solid var(--border);
    border-radius: var(--radius-sm); padding: 14px 16px;
    overflow-x: auto; white-space: pre-wrap; word-break: break-word;
    max-height: 400px; overflow-y: auto; color: #c9d1d9; line-height: 1.6;
  }

  /* Accordion */
  .accordion-item { border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 8px; overflow: hidden; transition: border-color 0.12s; }
  .accordion-item:hover { border-color: var(--border-bright); }
  .accordion-header { display: flex; align-items: center; gap: 12px; padding: 13px 16px; cursor: pointer; background: var(--bg-tertiary); user-select: none; transition: background 0.12s; }
  .accordion-header:hover { background: var(--bg-active); }
  .accordion-arrow { color: var(--text-muted); transition: transform 0.2s; font-size: 10px; }
  .accordion-item.open .accordion-arrow { transform: rotate(90deg); }
  .accordion-title { font-weight: 600; font-size: 13px; flex: 1; }
  .accordion-meta { font-size: 11px; color: var(--text-muted); }
  .accordion-module { font-size: 10px; color: var(--accent-light); background: rgba(109,40,217,0.15); padding: 2px 8px; border-radius: 8px; border: 1px solid rgba(109,40,217,0.2); }
  .accordion-body { display: none; padding: 16px 18px; background: var(--bg-secondary); border-top: 1px solid var(--border); }
  .accordion-item.open .accordion-body { display: block; }

  /* Table */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 9px 14px; font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid var(--border); background: var(--bg-primary); }
  td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: top; color: var(--text-primary); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--bg-hover); }
  .secret-value { color: var(--text-muted); font-family: var(--font-mono); font-size: 11px; letter-spacing: 1px; }
  .key-cell { font-family: var(--font-mono); font-size: 12px; color: var(--accent-light); white-space: nowrap; }
  .val-cell { font-family: var(--font-mono); font-size: 12px; word-break: break-all; color: var(--text-secondary); }

  /* Graph SVG */
  #graph-container { background: var(--bg-primary); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; min-height: 400px; overflow: auto; }
  .graph-node { fill: var(--bg-tertiary); stroke: var(--border-bright); stroke-width: 1.5; }
  .graph-node-router { fill: var(--accent-dim); stroke: var(--accent); }
  .graph-node-end { fill: #14532d; stroke: #166534; }
  .graph-node-text { fill: var(--text-primary); font-size: 12px; font-family: var(--font-mono); }
  .graph-edge { stroke: var(--text-muted); stroke-width: 1.5; fill: none; }
  .graph-edge-label { fill: var(--text-secondary); font-size: 10px; }

  /* Logs terminal */
  #log-terminal {
    background: #05050a; border: 1px solid var(--border);
    border-radius: var(--radius); height: 520px; overflow-y: auto;
    padding: 14px 16px; font-family: var(--font-mono); font-size: 12px;
    line-height: 1.6;
  }
  .log-entry { padding: 1px 0; }
  .log-entry .ts { color: #2e3048; margin-right: 8px; font-size: 11px; }
  .log-entry .logger-name { color: #4a5580; margin-right: 8px; }
  .log-DEBUG { color: #44475a; }
  .log-INFO { color: #50fa7b; }
  .log-WARNING { color: #f1fa8c; }
  .log-ERROR { color: #ff5555; }
  .log-CRITICAL { color: #ff79c6; font-weight: bold; }
  .log-controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
  .log-filter { display: flex; gap: 6px; align-items: center; }

  /* Tools list */
  .tool-item {
    background: var(--bg-primary); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px 18px; margin-bottom: 10px;
    transition: border-color 0.12s;
  }
  .tool-item:hover { border-color: var(--border-bright); }
  .tool-header { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 8px; }
  .tool-name { font-family: var(--font-mono); font-size: 13px; font-weight: 600; color: var(--accent-light); }
  .tool-module { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
  .tool-sig { font-family: var(--font-mono); font-size: 12px; color: #94a3b8; background: var(--bg-primary); border: 1px solid var(--border); padding: 6px 10px; border-radius: var(--radius-xs); margin-bottom: 8px; word-break: break-all; }
  .tool-doc { font-size: 12.5px; color: var(--text-secondary); line-height: 1.5; }

  /* MCP */
  .mcp-item {
    background: var(--bg-primary); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px 18px; margin-bottom: 10px;
    display: flex; align-items: flex-start; gap: 14px;
    transition: border-color 0.12s;
  }
  .mcp-item:hover { border-color: var(--border-bright); }
  .mcp-icon { font-size: 22px; margin-top: 2px; }
  .mcp-info { flex: 1; }
  .mcp-name { font-weight: 700; font-size: 14px; margin-bottom: 4px; }
  .mcp-cmd { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-secondary); margin-bottom: 4px; }
  .mcp-status { font-size: 12px; }

  /* Loading spinner */
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border-bright); border-top-color: var(--accent-light); border-radius: 50%; animation: spin 0.6s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Error box */
  .error-box {
    background: rgba(69,10,10,0.6); border: 1px solid rgba(127,29,29,0.6);
    border-radius: var(--radius-sm); padding: 12px 16px;
    color: #fca5a5; font-size: 13px; margin-top: 10px; line-height: 1.5;
  }

  /* Success box */
  .success-box {
    background: rgba(20,83,45,0.4); border: 1px solid rgba(22,101,52,0.6);
    border-radius: var(--radius-sm); padding: 12px 16px;
    color: #86efac; font-size: 13px; margin-top: 10px;
  }

  /* Modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); backdrop-filter: blur(4px); z-index: 1000; align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--bg-secondary); border: 1px solid var(--border-bright);
    border-radius: var(--radius); padding: 32px; width: 400px;
    box-shadow: var(--shadow-lg);
  }
  .modal-title { font-size: 16px; font-weight: 700; margin-bottom: 22px; }
  .modal-close { float: right; background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 20px; line-height: 1; margin-top: -4px; }
  .modal-close:hover { color: var(--text-primary); }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--bg-active); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

  /* Tab content */
  .tab-content { display: none; animation: fadeIn 0.15s ease; }
  .tab-content.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  /* Utilities */
  .mt-12 { margin-top: 12px; }
  .mt-16 { margin-top: 16px; }
  .flex { display: flex; }
  .gap-8 { gap: 8px; }
  .items-center { align-items: center; }
  .text-muted { color: var(--text-muted); }
  .text-sm { font-size: 12px; }
  .bold { font-weight: 600; }
  .copy-btn {
    background: var(--bg-active); border: 1px solid var(--border-bright);
    color: var(--text-secondary); padding: 3px 10px; border-radius: var(--radius-xs);
    cursor: pointer; font-size: 11px; font-family: var(--font-sans); transition: all 0.1s;
  }
  .copy-btn:hover { color: var(--text-primary); border-color: var(--text-muted); }
  .copy-btn.copied { color: var(--green); border-color: rgba(34,197,94,0.3); }

  /* Page title */
  .page-header { margin-bottom: 24px; }
  .page-title { font-size: 18px; font-weight: 700; letter-spacing: -0.4px; margin-bottom: 4px; }
  .page-subtitle { font-size: 13px; color: var(--text-muted); }
</style>
</head>
<body>

<!-- Login Modal -->
<div class="modal-overlay" id="loginModal">
  <div class="modal">
    <button class="modal-close" onclick="closeLoginModal()">&times;</button>
    <div class="modal-title">Iniciar sesión</div>
    <div class="form-row">
      <label>Email</label>
      <input type="email" id="loginEmail" placeholder="usuario@email.com" />
    </div>
    <div class="form-row">
      <label>Contraseña</label>
      <input type="password" id="loginPassword" placeholder="••••••••" />
    </div>
    <div id="loginError" class="error-box" style="display:none"></div>
    <div style="margin-top:16px; display:flex; gap:8px; justify-content:flex-end">
      <button class="btn btn-secondary" onclick="closeLoginModal()">Cancelar</button>
      <button class="btn btn-primary" onclick="doLogin()">Ingresar</button>
    </div>
  </div>
</div>

<div id="app">
  <!-- Sidebar -->
  <div id="sidebar">
    <div class="sidebar-brand">
      <div class="sidebar-brand-icon">⚡</div>
      <div class="sidebar-brand-text">
        CashIn
        <small>Debug Panel</small>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-label">Agente</div>
      <button class="tab-btn active" onclick="switchTab('agent')" data-tab="agent">
        <span class="tab-icon">🤖</span> Invocar Agente
      </button>
      <button class="tab-btn" onclick="switchTab('nodes')" data-tab="nodes">
        <span class="tab-icon">🔧</span> Nodos
      </button>
      <button class="tab-btn" onclick="switchTab('graph')" data-tab="graph">
        <span class="tab-icon">📊</span> Grafo
      </button>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-label">Inspección</div>
      <button class="tab-btn" onclick="switchTab('prompts')" data-tab="prompts">
        <span class="tab-icon">📝</span> Prompts
      </button>
      <button class="tab-btn" onclick="switchTab('tools')" data-tab="tools">
        <span class="tab-icon">🛠️</span> Tools
      </button>
      <button class="tab-btn" onclick="switchTab('config')" data-tab="config">
        <span class="tab-icon">⚙️</span> Config
      </button>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-label">Sistema</div>
      <button class="tab-btn" onclick="switchTab('logs')" data-tab="logs">
        <span class="tab-icon">📋</span> Logs
      </button>
      <button class="tab-btn" onclick="switchTab('mcp')" data-tab="mcp">
        <span class="tab-icon">🔌</span> MCP
      </button>
      <button class="tab-btn" onclick="switchTab('creditos')" data-tab="creditos">
        <span class="tab-icon">💳</span> Créditos
      </button>
    </div>
  </div>

  <!-- Main -->
  <div id="main">
    <div id="header">
      <div class="breadcrumb">
        <span>Debug</span>
        <span style="color:var(--border-bright)">›</span>
        <span class="current" id="currentTabLabel">Agente</span>
      </div>
      <div class="env-badge" id="envBadge">DEV</div>
      <div class="spacer"></div>
      <div class="user-info" id="userInfo"></div>
      <button class="btn-login" id="loginBtn" onclick="openLoginModal()">Login</button>
    </div>

    <div id="content">
      <!-- Agent Tab -->
      <div class="tab-content active" id="tab-agent">
        <div class="page-header">
          <div class="page-title">Invocar Agente</div>
          <div class="page-subtitle">Envía mensajes directamente al agente LangGraph sin autenticación</div>
        </div>
        <div class="card">
          <div class="card-title">Configuración</div>
          <div class="form-row">
            <label>Mensaje</label>
            <textarea id="agentMessage" rows="3" placeholder="Escribe un mensaje para el agente...">Hola, ¿qué puedes hacer?</textarea>
          </div>
          <div class="form-row">
            <label>Mode Override (opcional)</label>
            <select id="agentMode">
              <option value="">— auto (router decide) —</option>
              <option value="chat">chat</option>
              <option value="pipeline">pipeline</option>
              <option value="evidence">evidence</option>
              <option value="drive">drive</option>
              <option value="extract_obligations">extract_obligations</option>
              <option value="generate_activities">generate_activities</option>
              <option value="config">config</option>
            </select>
          </div>
          <div class="form-row">
            <label>Estado extra (JSON opcional)</label>
            <textarea id="agentExtraState" rows="3" placeholder='{"contrato_id_str": "...", "user_input": "..."}'></textarea>
          </div>
          <button class="btn btn-primary" id="agentInvokeBtn" onclick="invokeAgent()">▶ Invocar</button>
        </div>
        <div id="agentResult" style="display:none">
          <div class="card">
            <div class="card-title">Respuesta</div>
            <div class="response-bubble" id="agentResponse"></div>
            <div class="response-meta" id="agentMeta"></div>
          </div>
        </div>
        <div id="agentError" class="error-box" style="display:none"></div>
      </div>

      <!-- Nodes Tab -->
      <div class="tab-content" id="tab-nodes">
        <div class="card">
          <div class="card-title">Invocar Nodo Directamente</div>
          <div class="form-row">
            <label>Nodo</label>
            <select id="nodeSelect" class="node-dropdown">
              <option value="">Cargando nodos...</option>
            </select>
          </div>
          <div class="form-row">
            <label>State inicial (JSON)</label>
            <textarea id="nodeState" rows="8" placeholder='{"user_input": "test", "mode": "chat", "messages": [], "response": ""}'>{"user_input": "test", "mode": "chat", "messages": [], "response": ""}</textarea>
          </div>
          <button class="btn btn-primary" id="nodeInvokeBtn" onclick="invokeNode()">▶ Ejecutar Nodo</button>
        </div>
        <div id="nodeResult" style="display:none">
          <div class="card">
            <div class="card-title">State Resultante</div>
            <div class="response-meta" id="nodeMeta"></div>
            <pre id="nodeOutput" class="mt-12"></pre>
          </div>
        </div>
        <div id="nodeError" class="error-box" style="display:none"></div>
      </div>

      <!-- Prompts Tab -->
      <div class="tab-content" id="tab-prompts">
        <div class="card">
          <div class="card-title">Prompts del Agente</div>
          <div id="promptsList"><div class="text-muted">Cargando...</div></div>
        </div>
      </div>

      <!-- Tools Tab -->
      <div class="tab-content" id="tab-tools">
        <div class="card">
          <div class="card-title">Agent Tools</div>
          <div id="toolsList"><div class="text-muted">Cargando...</div></div>
        </div>
      </div>

      <!-- Config Tab -->
      <div class="tab-content" id="tab-config">
        <div class="card">
          <div class="card-title">Configuración (secrets enmascarados)</div>
          <div style="overflow-x:auto">
            <table id="configTable">
              <thead><tr><th>Clave</th><th>Valor</th></tr></thead>
              <tbody><tr><td colspan="2" class="text-muted">Cargando...</td></tr></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Graph Tab -->
      <div class="tab-content" id="tab-graph">
        <div class="card">
          <div class="card-title">Workflow del Agente</div>
          <div id="graph-container">
            <div class="text-muted">Cargando...</div>
          </div>
        </div>
      </div>

      <!-- Logs Tab -->
      <div class="tab-content" id="tab-logs">
        <div class="card">
          <div class="card-title">Logs en Tiempo Real</div>
          <div class="log-controls">
            <button class="btn btn-secondary btn-sm" onclick="clearLogs()">🗑 Clear</button>
            <button class="btn btn-secondary btn-sm" id="pauseLogBtn" onclick="toggleLogPause()">⏸ Pausar</button>
            <div class="log-filter">
              <span class="text-muted text-sm">Filtrar:</span>
              <select id="logLevelFilter" onchange="applyLogFilter()" style="width:auto; padding: 4px 8px;">
                <option value="">Todo</option>
                <option value="DEBUG">DEBUG</option>
                <option value="INFO">INFO</option>
                <option value="WARNING">WARNING</option>
                <option value="ERROR">ERROR</option>
              </select>
            </div>
            <div id="logStatus" class="text-muted text-sm">● Conectando...</div>
          </div>
          <div id="log-terminal"></div>
        </div>
      </div>

      <!-- MCP Tab -->
      <div class="tab-content" id="tab-mcp">
        <div class="card">
          <div class="card-title">MCP Servers</div>
          <div id="mcpList"><div class="text-muted">Cargando...</div></div>
        </div>
      </div>

      <!-- Créditos Tab (dev-only testing tool) -->
      <div class="tab-content" id="tab-creditos">
        <div class="page-header">
          <div class="page-title">Créditos — Herramienta de Testing</div>
          <div class="page-subtitle">Solo disponible en desarrollo. No existe en producción.</div>
        </div>
        <div class="card" id="creditosBalanceCard">
          <div class="card-title">Balance actual</div>
          <div id="creditosBalanceContent"><div class="text-muted">Iniciá sesión y cargá el balance.</div></div>
          <button class="btn btn-secondary btn-sm mt-12" onclick="loadCreditosBalance()">↻ Refrescar balance</button>
        </div>
        <div class="card">
          <div class="card-title">Agregar créditos (testing)</div>
          <div class="form-row">
            <label>Cantidad a agregar</label>
            <input type="text" id="creditosCantidad" placeholder="ej: 30" value="30" />
          </div>
          <div class="form-row">
            <label>Nota (opcional)</label>
            <input type="text" id="creditosNota" placeholder="ej: prueba de cuenta de cobro" />
          </div>
          <button class="btn btn-primary" id="creditosAgregarBtn" onclick="agregarCreditos()">+ Agregar créditos</button>
          <div id="creditosResult" class="success-box mt-12" style="display:none"></div>
          <div id="creditosError" class="error-box mt-12" style="display:none"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
// ─────────────────────────────────────────────
// State
// ─────────────────────────────────────────────
let _token = sessionStorage.getItem('debug_token') || '';
let _userEmail = sessionStorage.getItem('debug_email') || '';
let _logPaused = false;
let _logFilter = '';
let _logSource = null;
let _logBuffer = [];
let _loadedTabs = new Set(['agent']);

// ─────────────────────────────────────────────
// Bootstrap
// ─────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  updateAuthUI();
  loadInfo();
});

async function loadInfo() {
  try {
    const d = await api('/debug/info');
    document.getElementById('envBadge').textContent = (d.environment || 'DEV').toUpperCase();
  } catch(e) {}
}

// ─────────────────────────────────────────────
// Auth
// ─────────────────────────────────────────────
function openLoginModal() { document.getElementById('loginModal').classList.add('open'); }
function closeLoginModal() { document.getElementById('loginModal').classList.remove('open'); document.getElementById('loginError').style.display='none'; }

async function doLogin() {
  const email = document.getElementById('loginEmail').value.trim();
  const pass = document.getElementById('loginPassword').value;
  const errEl = document.getElementById('loginError');
  errEl.style.display = 'none';
  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password: pass})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Login failed');
    _token = data.access_token;
    _userEmail = email;
    sessionStorage.setItem('debug_token', _token);
    sessionStorage.setItem('debug_email', _userEmail);
    updateAuthUI();
    closeLoginModal();
  } catch(e) {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
  }
}

function updateAuthUI() {
  const btn = document.getElementById('loginBtn');
  const info = document.getElementById('userInfo');
  if (_token) {
    btn.textContent = 'Logout';
    btn.onclick = doLogout;
    info.textContent = _userEmail;
  } else {
    btn.textContent = 'Login';
    btn.onclick = openLoginModal;
    info.textContent = '';
  }
}

function doLogout() {
  _token = ''; _userEmail = '';
  sessionStorage.removeItem('debug_token'); sessionStorage.removeItem('debug_email');
  updateAuthUI();
}

// ─────────────────────────────────────────────
// API helper
// ─────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = {'Content-Type': 'application/json', ...(opts.headers || {})};
  if (_token) headers['Authorization'] = 'Bearer ' + _token;
  const res = await fetch(path, {...opts, headers});
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || res.statusText);
  }
  return res.json();
}

// ─────────────────────────────────────────────
// Tab switching + lazy loading
// ─────────────────────────────────────────────
const TAB_LOADERS = {
  nodes: loadNodes,
  prompts: loadPrompts,
  tools: loadTools,
  config: loadConfig,
  graph: loadGraph,
  logs: initLogs,
  mcp: loadMCP,
  creditos: loadCreditosBalance,
};

const TAB_LABELS = {
  agent: 'Agente', nodes: 'Nodos', graph: 'Grafo',
  prompts: 'Prompts', tools: 'Tools', config: 'Config',
  logs: 'Logs', mcp: 'MCP', creditos: 'Créditos'
};
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
  const lbl = document.getElementById('currentTabLabel');
  if (lbl) lbl.textContent = TAB_LABELS[name] || name;
  if (!_loadedTabs.has(name) && TAB_LOADERS[name]) {
    _loadedTabs.add(name);
    TAB_LOADERS[name]();
  }
}

// ─────────────────────────────────────────────
// Agent invoke
// ─────────────────────────────────────────────
async function invokeAgent() {
  const btn = document.getElementById('agentInvokeBtn');
  const resultEl = document.getElementById('agentResult');
  const errorEl = document.getElementById('agentError');
  const responseEl = document.getElementById('agentResponse');
  const metaEl = document.getElementById('agentMeta');

  errorEl.style.display = 'none';
  resultEl.style.display = 'none';
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Invocando...';

  try {
    const message = document.getElementById('agentMessage').value;
    const modeOverride = document.getElementById('agentMode').value || null;
    let extraState = null;
    const extraRaw = document.getElementById('agentExtraState').value.trim();
    if (extraRaw) {
      try { extraState = JSON.parse(extraRaw); } catch(e) { throw new Error('Extra state JSON inválido: ' + e.message); }
    }

    const data = await api('/debug/agent/invoke', {
      method: 'POST',
      body: JSON.stringify({message, mode_override: modeOverride, extra_state: extraState})
    });

    responseEl.textContent = data.response || '(sin respuesta)';
    metaEl.innerHTML = `
      <div class="meta-item">Modo: <span>${data.mode}</span></div>
      <div class="meta-item">Duración: <span>${data.duration_ms}ms</span></div>
      ${data.error ? '<div class="meta-item" style="color:var(--red)">Error: <span>' + escHtml(data.error) + '</span></div>' : ''}
      <div class="meta-item text-muted">Keys: <span>${(data.state_keys || []).join(', ')}</span></div>
    `;
    resultEl.style.display = 'block';
  } catch(e) {
    errorEl.textContent = e.message;
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Invocar';
  }
}

// ─────────────────────────────────────────────
// Node invoke
// ─────────────────────────────────────────────
async function loadNodes() {
  try {
    const nodes = await api('/debug/nodes');
    const sel = document.getElementById('nodeSelect');
    sel.innerHTML = nodes.map(n => `<option value="${n.name}">${n.name}</option>`).join('');
  } catch(e) {
    document.getElementById('nodeSelect').innerHTML = '<option>Error al cargar</option>';
  }
}

async function invokeNode() {
  const btn = document.getElementById('nodeInvokeBtn');
  const resultEl = document.getElementById('nodeResult');
  const errorEl = document.getElementById('nodeError');
  const outputEl = document.getElementById('nodeOutput');
  const metaEl = document.getElementById('nodeMeta');

  errorEl.style.display = 'none';
  resultEl.style.display = 'none';
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Ejecutando...';

  try {
    const nodeName = document.getElementById('nodeSelect').value;
    if (!nodeName) throw new Error('Selecciona un nodo');
    let stateObj = {};
    const stateRaw = document.getElementById('nodeState').value.trim();
    if (stateRaw) { try { stateObj = JSON.parse(stateRaw); } catch(e) { throw new Error('State JSON inválido: ' + e.message); } }

    const data = await api('/debug/node/invoke', {
      method: 'POST',
      body: JSON.stringify({node_name: nodeName, state: stateObj})
    });

    metaEl.innerHTML = `<div class="meta-item">Nodo: <span>${data.node}</span></div><div class="meta-item">Duración: <span>${data.duration_ms}ms</span></div>`;
    outputEl.textContent = JSON.stringify(data.state, null, 2);
    resultEl.style.display = 'block';
  } catch(e) {
    errorEl.textContent = e.message;
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Ejecutar Nodo';
  }
}

// ─────────────────────────────────────────────
// Prompts
// ─────────────────────────────────────────────
async function loadPrompts() {
  const el = document.getElementById('promptsList');
  try {
    const prompts = await api('/debug/prompts');
    if (!prompts.length) { el.innerHTML = '<div class="text-muted">Sin prompts encontrados.</div>'; return; }
    el.innerHTML = prompts.map((p, i) => `
      <div class="accordion-item" id="acc-${i}">
        <div class="accordion-header" onclick="toggleAccordion('acc-${i}')">
          <span class="accordion-arrow">▶</span>
          <span class="accordion-title">${escHtml(p.name)}</span>
          <span class="accordion-module">${escHtml(p.module)}</span>
          <span class="accordion-meta">${p.length} chars</span>
        </div>
        <div class="accordion-body">
          <div style="display:flex; justify-content:flex-end; margin-bottom:8px">
            <button class="copy-btn" onclick="copyText('prompt-pre-${i}')">Copiar</button>
          </div>
          <pre id="prompt-pre-${i}">${escHtml(p.content)}</pre>
        </div>
      </div>
    `).join('');
  } catch(e) {
    el.innerHTML = `<div class="error-box">${e.message}</div>`;
  }
}

function toggleAccordion(id) {
  document.getElementById(id).classList.toggle('open');
}

// ─────────────────────────────────────────────
// Tools
// ─────────────────────────────────────────────
async function loadTools() {
  const el = document.getElementById('toolsList');
  try {
    const tools = await api('/debug/tools');
    if (!tools.length) { el.innerHTML = '<div class="text-muted">Sin tools encontradas.</div>'; return; }
    el.innerHTML = tools.map(t => `
      <div class="tool-item">
        <div class="tool-header">
          <div style="flex:1">
            <div class="tool-name">${escHtml(t.name)}</div>
            <div class="tool-module">${escHtml(t.module)}</div>
          </div>
          <span class="badge ${t.is_async ? 'badge-async' : 'badge-sync'}">${t.is_async ? 'async' : 'sync'}</span>
        </div>
        <div class="tool-sig">${escHtml(t.signature)}</div>
        ${t.doc ? `<div class="tool-doc">${escHtml(t.doc)}</div>` : ''}
      </div>
    `).join('');
  } catch(e) {
    el.innerHTML = `<div class="error-box">${e.message}</div>`;
  }
}

// ─────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────
async function loadConfig() {
  const tbody = document.querySelector('#configTable tbody');
  try {
    const cfg = await api('/debug/config');
    const rows = Object.entries(cfg).map(([k, v]) => {
      const isSecret = v === '****';
      const displayVal = isSecret
        ? '<span class="secret-value">****</span>'
        : `<span class="val-cell">${escHtml(JSON.stringify(v))}</span>`;
      return `<tr><td class="key-cell">${escHtml(k)}</td><td>${displayVal}</td></tr>`;
    });
    tbody.innerHTML = rows.join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="2" class="error-box">${e.message}</td></tr>`;
  }
}

// ─────────────────────────────────────────────
// Graph visualization
// ─────────────────────────────────────────────
async function loadGraph() {
  const container = document.getElementById('graph-container');
  try {
    await api('/debug/graph');
    // Static graph visualization based on known CashIn graph structure
    container.innerHTML = renderGraphSVG();
  } catch(e) {
    container.innerHTML = `<div class="error-box">${e.message}</div>`;
  }
}

function renderGraphSVG() {
  // Node layout: column → [nodes]
  const nodeW = 160, nodeH = 36, hGap = 60, vGap = 20;
  const cols = [
    [{id:'router', label:'router', type:'router'}],
    [{id:'chat', label:'chat', type:'normal'}, {id:'doc_ingestion', label:'doc_ingestion', type:'normal'}, {id:'email_fetch', label:'email_fetch', type:'normal'}, {id:'drive_upload', label:'drive_upload', type:'normal'}, {id:'extraction_router', label:'extraction_router', type:'normal'}, {id:'generate_activities', label:'generate_activities', type:'normal'}],
    [{id:'doc_understanding', label:'doc_understanding', type:'normal'}, {id:'contract_metadata', label:'contract_metadata', type:'normal'}],
    [{id:'classification', label:'classification', type:'normal'}, {id:'obligations_extraction', label:'obligations_extraction', type:'normal'}],
    [{id:'justification', label:'justification', type:'normal'}],
    [{id:'END', label:'END', type:'end'}],
  ];

  // Compute positions
  const positions = {};
  let x = 30;
  cols.forEach((col, ci) => {
    const totalH = col.length * nodeH + (col.length - 1) * vGap;
    const startY = 30 + Math.max(0, (cols[1].length * (nodeH + vGap) - totalH) / 2);
    col.forEach((n, ni) => {
      positions[n.id] = {x, y: startY + ni * (nodeH + vGap), w: nodeW, h: nodeH};
    });
    x += nodeW + hGap;
  });

  const edges = [
    ['router','chat'],['router','doc_ingestion'],['router','email_fetch'],['router','drive_upload'],['router','extraction_router'],['router','generate_activities'],
    ['chat','END'],['doc_ingestion','doc_understanding'],['doc_understanding','classification'],['classification','justification'],['justification','END'],
    ['email_fetch','END'],['drive_upload','END'],
    ['extraction_router','contract_metadata'],['extraction_router','obligations_extraction'],
    ['contract_metadata','obligations_extraction'],['obligations_extraction','END'],
    ['generate_activities','END'],
  ];

  const svgW = x + 20;
  const svgH = cols[1].length * (nodeH + vGap) + 80;

  let svg = `<svg width="${svgW}" height="${svgH}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
      <path d="M0,0 L0,6 L8,3 z" fill="#555e80"/>
    </marker>
  </defs>`;

  // Draw edges first
  edges.forEach(([from, to]) => {
    const fp = positions[from], tp = positions[to];
    if (!fp || !tp) return;
    const fx = fp.x + fp.w, fy = fp.y + fp.h / 2;
    const tx = tp.x, ty = tp.y + tp.h / 2;
    const mx = (fx + tx) / 2;
    svg += `<path d="M${fx},${fy} C${mx},${fy} ${mx},${ty} ${tx},${ty}" class="graph-edge" marker-end="url(#arr)" stroke="#555e80" stroke-width="1.5" fill="none"/>`;
  });

  // Draw nodes
  const allNodes = cols.flat();
  allNodes.forEach(n => {
    const p = positions[n.id];
    if (!p) return;
    let fill = '#22263a', stroke = '#2e3250';
    if (n.type === 'router') { fill = '#3d1f78'; stroke = '#7c3aed'; }
    if (n.type === 'end') { fill = '#14532d'; stroke = '#166534'; }
    svg += `<rect x="${p.x}" y="${p.y}" width="${p.w}" height="${p.h}" rx="6" fill="${fill}" stroke="${stroke}" stroke-width="1.5"/>`;
    svg += `<text x="${p.x + p.w/2}" y="${p.y + p.h/2 + 4}" text-anchor="middle" fill="#e2e4f0" font-size="12" font-family="monospace">${escHtml(n.label)}</text>`;
  });

  svg += '</svg>';
  return svg;
}

// ─────────────────────────────────────────────
// Logs
// ─────────────────────────────────────────────
function initLogs() {
  connectLogStream();
}

function connectLogStream() {
  if (_logSource) { _logSource.close(); _logSource = null; }
  const statusEl = document.getElementById('logStatus');
  statusEl.textContent = '● Conectando...';
  statusEl.style.color = 'var(--yellow)';

  _logSource = new EventSource('/debug/logs/stream');
  _logSource.onopen = () => {
    statusEl.textContent = '● Conectado';
    statusEl.style.color = 'var(--green)';
  };
  _logSource.onerror = () => {
    statusEl.textContent = '● Desconectado';
    statusEl.style.color = 'var(--red)';
  };
  _logSource.onmessage = (e) => {
    try {
      const entry = JSON.parse(e.data);
      appendLogEntry(entry);
    } catch(err) {}
  };
}

function appendLogEntry(entry) {
  if (_logPaused) { _logBuffer.push(entry); return; }
  const filter = document.getElementById('logLevelFilter').value;
  if (filter && entry.level !== filter) return;
  const term = document.getElementById('log-terminal');
  const div = document.createElement('div');
  div.className = `log-entry log-${entry.level}`;
  const ts = new Date(entry.ts).toISOString().slice(11, 23);
  div.innerHTML = `<span class="ts">${ts}</span><span class="logger-name">${escHtml(entry.logger || '')}</span>${escHtml(entry.message || '')}`;
  term.appendChild(div);
  // Auto-scroll
  if (term.scrollTop + term.clientHeight >= term.scrollHeight - 40) {
    term.scrollTop = term.scrollHeight;
  }
  // Trim to 1000 entries
  while (term.children.length > 1000) term.removeChild(term.firstChild);
}

function clearLogs() {
  document.getElementById('log-terminal').innerHTML = '';
  _logBuffer = [];
}

function toggleLogPause() {
  _logPaused = !_logPaused;
  const btn = document.getElementById('pauseLogBtn');
  if (_logPaused) {
    btn.textContent = '▶ Reanudar';
    btn.style.color = 'var(--yellow)';
  } else {
    btn.textContent = '⏸ Pausar';
    btn.style.color = '';
    // Flush buffer
    _logBuffer.forEach(e => appendLogEntry(e));
    _logBuffer = [];
  }
}

function applyLogFilter() {
  // Just reconnect will apply filter on new messages; clear + re-render history
  clearLogs();
}

// ─────────────────────────────────────────────
// MCP
// ─────────────────────────────────────────────
async function loadMCP() {
  const el = document.getElementById('mcpList');
  try {
    const data = await api('/debug/mcp/config');
    if (!data.files || !data.files.length) {
      el.innerHTML = '<div class="text-muted">No se encontraron MCP servers configurados.</div>';
      return;
    }
    el.innerHTML = data.files.map(s => `
      <div class="mcp-item">
        <div class="mcp-icon">${s.exists ? '🟢' : '🔴'}</div>
        <div class="mcp-info">
          <div class="mcp-name">${escHtml(s.name)}</div>
          <div class="mcp-cmd">${s.command ? escHtml(s.command + ' ' + (s.args || []).join(' ')) : '<span class="text-muted">sin comando configurado</span>'}</div>
          <div class="mcp-status">
            <span class="badge ${s.exists ? 'badge-green' : 'badge-red'}">${s.exists ? 'Archivo existe' : 'Archivo no encontrado'}</span>
            <span style="margin-left:8px; color:var(--text-muted); font-size:12px">${escHtml(s.file)}</span>
          </div>
        </div>
      </div>
    `).join('');

    // Config raw
    if (Object.keys(data.mcp_servers).length) {
      el.innerHTML += `
        <div class="card" style="margin-top:16px">
          <div class="card-title">settings.json — mcpServers</div>
          <pre>${escHtml(JSON.stringify(data.mcp_servers, null, 2))}</pre>
        </div>`;
    }
  } catch(e) {
    el.innerHTML = `<div class="error-box">${e.message}</div>`;
  }
}

// ─────────────────────────────────────────────
// Créditos (dev-only testing tool)
// ─────────────────────────────────────────────
async function loadCreditosBalance() {
  const el = document.getElementById('creditosBalanceContent');
  if (!_token) { el.innerHTML = '<div class="error-box">Iniciá sesión primero (botón Login arriba).</div>'; return; }
  el.innerHTML = '<span class="spinner"></span> Cargando...';
  try {
    const d = await api('/debug/creditos/balance');
    el.innerHTML = `
      <table style="width:auto; min-width:340px">
        <tr><td style="color:var(--text-muted); padding:6px 14px 6px 0">Usuario</td><td class="val-cell">${escHtml(d.email)}</td></tr>
        <tr><td style="color:var(--text-muted); padding:6px 14px 6px 0">Ingreso total</td><td class="val-cell" style="color:var(--green)">+${d.ingreso_total}</td></tr>
        <tr><td style="color:var(--text-muted); padding:6px 14px 6px 0">Consumido total</td><td class="val-cell" style="color:var(--red)">-${d.consumido_total}</td></tr>
        <tr><td style="color:var(--text-muted); padding:6px 14px 6px 0"><b>Saldo real</b></td><td class="val-cell" style="color:var(--accent-light); font-weight:700; font-size:16px">${d.saldo_real}</td></tr>
        <tr><td style="color:var(--text-muted); padding:6px 14px 6px 0">Cache DB (creditos_disponibles)</td><td class="val-cell" style="color:var(--yellow)">${d.creditos_disponibles_cache}</td></tr>
      </table>
      ${d.saldo_real !== d.creditos_disponibles_cache ? '<div class="error-box" style="margin-top:10px">⚠ El saldo real difiere del cache. El saldo real es el correcto.</div>' : ''}
    `;
  } catch(e) {
    el.innerHTML = `<div class="error-box">${escHtml(e.message)}</div>`;
  }
}

async function agregarCreditos() {
  if (!_token) { alert('Iniciá sesión primero.'); return; }
  const btn = document.getElementById('creditosAgregarBtn');
  const resultEl = document.getElementById('creditosResult');
  const errorEl = document.getElementById('creditosError');
  resultEl.style.display = 'none';
  errorEl.style.display = 'none';
  const cantidad = parseInt(document.getElementById('creditosCantidad').value, 10);
  if (!cantidad || cantidad <= 0) { errorEl.textContent = 'Ingresá una cantidad válida (> 0).'; errorEl.style.display='block'; return; }
  const nota = document.getElementById('creditosNota').value.trim() || null;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Agregando...';
  try {
    const d = await api('/debug/creditos/agregar', { method: 'POST', body: JSON.stringify({ cantidad, nota }) });
    resultEl.textContent = `✓ ${d.message}. Cache actualizado: ${d.creditos_disponibles_cache} créditos.`;
    resultEl.style.display = 'block';
    loadCreditosBalance();
  } catch(e) {
    errorEl.textContent = e.message;
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = '+ Agregar créditos';
  }
}

// ─────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────
function escHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function copyText(preId) {
  const el = document.getElementById(preId);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(() => {
    const btn = el.parentElement && el.parentElement.querySelector('.copy-btn');
    if (btn) {
      const old = btn.textContent;
      btn.textContent = '✓ Copiado';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = old; btn.classList.remove('copied'); }, 1400);
    }
  });
}
</script>
</body>
</html>"""
