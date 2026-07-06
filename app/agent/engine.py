"""Custom async graph orchestration engine — replaces LangGraph."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.agent.state import AgentState

NodeFn = Callable[[AgentState], AgentState | Awaitable[AgentState]]
RouterFn = Callable[[AgentState], str]

MAX_STEPS = 50


class _End:
    """Singleton sentinel marking graph termination."""

    _instance: ClassVar[_End | None] = None

    def __new__(cls) -> _End:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "END"


END = _End()


@dataclass
class HumanInterrupt(Exception):
    """Raised by HIL nodes to pause graph execution and request user input."""

    message: str
    node: str | None = None


@dataclass
class RunResult:
    """Result of a graph run() call."""

    state: AgentState
    status: str  # "completed" | "paused"
    paused_node: str | None = None
    interrupt_message: str | None = None


class _EdgeView:
    """Compatibility shim — debug.py reads .graph.edges."""

    def __init__(self, edges: set[tuple[str, str]]) -> None:
        self._edges = edges

    @property
    def edges(self) -> set[tuple[str, str]]:
        return self._edges


class CompiledGraph:
    """Async graph engine with StateGraph-compatible builder API."""

    def __init__(self) -> None:
        self._nodes: dict[str, NodeFn] = {}
        self._static_edges: dict[str, str | _End] = {}
        self._conditional_edges: dict[str, tuple[RouterFn, dict[str, str] | None]] = {}
        self._entry: str | None = None
        self._checkpointer: Any = None  # unused; kept for call-site compat

    # ── Builder API (mirrors StateGraph) ──────────────────────────────────────

    def add_node(self, name: str, fn: NodeFn) -> None:
        self._nodes[name] = fn

    def add_edge(self, src: str, dst: str | _End) -> None:
        self._static_edges[src] = dst

    def add_conditional_edges(
        self,
        src: str,
        router: RouterFn,
        mapping: dict[str, str] | None = None,
    ) -> None:
        self._conditional_edges[src] = (router, mapping)

    def set_entry_point(self, name: str) -> None:
        self._entry = name

    def compile(self, checkpointer: Any = None) -> "CompiledGraph":
        """Validate topology and return self."""
        if self._entry is None:
            raise ValueError("Entry point not set — call set_entry_point() first")
        self._checkpointer = checkpointer
        return self

    # ── Execution ─────────────────────────────────────────────────────────────

    async def run(
        self,
        state: AgentState,
        *,
        start_node: str | None = None,
    ) -> RunResult:
        """Execute graph from start_node (or entry) until END or HumanInterrupt."""
        current: str | None = start_node or self._entry
        if current is None:
            raise RuntimeError("No entry point configured")

        for _ in range(MAX_STEPS):
            fn = self._nodes[current]
            try:
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(state)
                else:
                    result = fn(state)
            except HumanInterrupt as exc:
                exc.node = current
                return RunResult(
                    state=state,
                    status="paused",
                    paused_node=current,
                    interrupt_message=exc.message,
                )

            state = {**state, **result}  # type: ignore[misc]

            # Determine next node
            next_node: str | _End | None = None
            if current in self._conditional_edges:
                router_fn, mapping = self._conditional_edges[current]
                route_key = router_fn(state)
                if mapping:
                    raw = mapping.get(route_key, route_key)
                else:
                    raw = route_key
                next_node = END if (raw is END or raw == "END" or isinstance(raw, _End)) else raw
            elif current in self._static_edges:
                next_node = self._static_edges[current]

            if next_node is None or isinstance(next_node, _End):
                return RunResult(state=state, status="completed")

            current = next_node  # type: ignore[assignment]

        raise RuntimeError(f"Graph exceeded {MAX_STEPS} steps — possible cycle detected")

    async def ainvoke(
        self,
        state: AgentState,
        config: Any = None,  # ignored; LangGraph compat shim
        *,
        start_node: str | None = None,
    ) -> AgentState:
        """LangGraph-compatible invoke. Returns final state dict."""
        result = await self.run(state, start_node=start_node)
        return result.state

    # ── Compatibility properties ───────────────────────────────────────────────

    @property
    def nodes(self) -> dict[str, NodeFn]:
        return self._nodes

    @property
    def graph(self) -> _EdgeView:
        """debug.py reads .graph.edges — returns static edges as set[tuple[str,str]]."""
        edges: set[tuple[str, str]] = set()
        for src, dst in self._static_edges.items():
            if not isinstance(dst, _End):
                edges.add((src, str(dst)))
        return _EdgeView(edges)
