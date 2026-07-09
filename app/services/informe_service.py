"""Informe service — generate DOCX reports and ZIP evidence folder for a CuentaCobro.

Produces three artifacts on demand for the contractor's billing package:
1. Informe de actividades del contratista (DOCX)
2. Informe de supervisión (DOCX)
3. Carpeta de evidencias (ZIP) — folder structure with placeholders per obligation.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date

import structlog
from docx import Document
from docx.shared import Pt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ACTIVIDADES_MISSING, ForbiddenError, NotFoundError, ValidationError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.obligacion import Obligacion
from app.models.usuario import Usuario

logger = structlog.get_logger("service.informe")

_MESES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


# ── Internal helpers ────────────────────────────────────────────────────────


async def _load_context(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> tuple[CuentaCobro, Contrato, Usuario, list[Obligacion], list[Actividad]]:
    """Load all data needed to render the informes, validating ownership."""
    result = await db.execute(
        select(CuentaCobro)
        .options(
            selectinload(CuentaCobro.contrato).selectinload(Contrato.obligaciones),
            selectinload(CuentaCobro.actividades).selectinload(Actividad.evidencias),
        )
        .where(CuentaCobro.id == cuenta_id, CuentaCobro.deleted_at.is_(None))
    )
    cuenta = result.scalar_one_or_none()
    if cuenta is None:
        raise NotFoundError("CuentaCobro", str(cuenta_id))
    if cuenta.contrato.usuario_id != usuario_id:
        raise ForbiddenError()

    user_result = await db.execute(select(Usuario).where(Usuario.id == usuario_id))
    usuario = user_result.scalar_one_or_none()
    if usuario is None:
        raise NotFoundError("Usuario", str(usuario_id))

    obligaciones = sorted(cuenta.contrato.obligaciones, key=lambda o: o.orden)
    actividades = sorted(cuenta.actividades, key=lambda a: a.created_at)
    return cuenta, cuenta.contrato, usuario, obligaciones, actividades


def _periodo_str(cuenta: CuentaCobro) -> str:
    return f"{_MESES[cuenta.mes - 1]} de {cuenta.anio}"


def _formato_fecha(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def _formato_valor(v: float) -> str:
    return f"$ {float(v):,.2f}"


def _add_kv_table(doc: Document, rows: list[tuple[str, str]]) -> None:
    """Add a 2-column key/value table for header info."""
    table = doc.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"
    for i, (k, v) in enumerate(rows):
        table.cell(i, 0).text = k
        table.cell(i, 1).text = v
        # Bold the key column
        for paragraph in table.cell(i, 0).paragraphs:
            for run in paragraph.runs:
                run.bold = True


def _add_actividades_table(
    doc: Document,
    actividades: list[Actividad],
    obligaciones_by_id: dict[uuid.UUID, Obligacion],
) -> None:
    """Render activities as a bordered table: # | Obligación | Actividad | Justificación."""
    headers = ["#", "Obligación", "Actividad realizada", "Justificación"]
    table = doc.add_table(rows=1 + len(actividades), cols=len(headers))
    table.style = "Table Grid"
    for i, h in enumerate(headers):
        cell = table.cell(0, i)
        cell.text = h
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True

    for idx, act in enumerate(actividades, start=1):
        row = table.rows[idx]
        ob = obligaciones_by_id.get(act.obligacion_id) if act.obligacion_id else None
        ob_text = ob.descripcion if ob else "—"
        row.cells[0].text = str(idx)
        row.cells[1].text = ob_text
        row.cells[2].text = act.descripcion
        row.cells[3].text = act.justificacion or "—"


# ── Public API ──────────────────────────────────────────────────────────────


async def generar_informe_actividades_docx(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> tuple[bytes, str]:
    """Generate the contractor's activities report as DOCX. Returns (bytes, filename)."""
    cuenta, contrato, usuario, obligaciones, actividades = await _load_context(
        db, usuario_id, cuenta_id
    )
    if not actividades:
        raise ValidationError(
            "No hay actividades registradas. Genera o ingresa actividades antes de descargar el informe.",
            code=ACTIVIDADES_MISSING,
        )

    obligaciones_by_id = {ob.id: ob for ob in obligaciones}

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    title = doc.add_heading("Informe de actividades del contratista", level=1)
    title.alignment = 1  # center
    doc.add_paragraph(
        f"Período reportado: {_periodo_str(cuenta)}"
    ).alignment = 1
    doc.add_paragraph()

    _add_kv_table(
        doc,
        [
            ("Entidad", contrato.entidad or "—"),
            ("Dependencia", contrato.dependencia or "—"),
            ("N° Contrato", contrato.numero_contrato),
            ("Contratista", usuario.nombre),
            ("C.C.", usuario.cedula or "—"),
            ("Supervisor", contrato.supervisor_nombre or "—"),
            ("Objeto", contrato.objeto),
            ("Período", _periodo_str(cuenta)),
            ("Valor a cobrar", _formato_valor(cuenta.valor)),
        ],
    )

    doc.add_paragraph()
    doc.add_heading("Actividades realizadas", level=2)
    _add_actividades_table(doc, actividades, obligaciones_by_id)

    doc.add_paragraph()
    doc.add_paragraph(
        "Certifico que las actividades aquí relacionadas fueron ejecutadas en cumplimiento de las "
        "obligaciones contractuales pactadas durante el período reportado."
    )
    doc.add_paragraph()
    doc.add_paragraph()
    doc.add_paragraph("_______________________________")
    doc.add_paragraph(usuario.nombre)
    doc.add_paragraph(f"C.C. {usuario.cedula or '—'}")
    doc.add_paragraph("Contratista")

    buf = io.BytesIO()
    doc.save(buf)

    filename = (
        f"informe-actividades-{contrato.numero_contrato}-"
        f"{cuenta.anio}-{cuenta.mes:02d}.docx"
    )
    await logger.ainfo(
        "informe_actividades_generado",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        size=len(buf.getvalue()),
    )
    return buf.getvalue(), filename


async def generar_informe_supervision_docx(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> tuple[bytes, str]:
    """Generate the supervisor's report as DOCX. Returns (bytes, filename)."""
    cuenta, contrato, usuario, obligaciones, actividades = await _load_context(
        db, usuario_id, cuenta_id
    )
    if not actividades:
        raise ValidationError(
            "No hay actividades registradas. Genera o ingresa actividades antes de descargar el informe.",
            code=ACTIVIDADES_MISSING,
        )

    obligaciones_by_id = {ob.id: ob for ob in obligaciones}

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    title = doc.add_heading("Informe de supervisión", level=1)
    title.alignment = 1
    doc.add_paragraph(f"Período supervisado: {_periodo_str(cuenta)}").alignment = 1
    doc.add_paragraph()

    _add_kv_table(
        doc,
        [
            ("Entidad", contrato.entidad or "—"),
            ("Dependencia", contrato.dependencia or "—"),
            ("N° Contrato", contrato.numero_contrato),
            ("Contratista", usuario.nombre),
            ("C.C.", usuario.cedula or "—"),
            ("Supervisor", contrato.supervisor_nombre or "—"),
            ("Objeto", contrato.objeto),
            ("Período", _periodo_str(cuenta)),
            ("Valor autorizado", _formato_valor(cuenta.valor)),
        ],
    )

    doc.add_paragraph()
    doc.add_heading("Verificación del cumplimiento", level=2)
    doc.add_paragraph(
        "El supervisor del contrato certifica que ha verificado el cumplimiento de las obligaciones "
        "contractuales por parte del contratista durante el período reportado, con base en las "
        "actividades y evidencias relacionadas a continuación."
    )

    doc.add_heading("Actividades verificadas", level=2)
    _add_actividades_table(doc, actividades, obligaciones_by_id)

    doc.add_paragraph()
    doc.add_heading("Concepto del supervisor", level=2)
    doc.add_paragraph(
        "Las actividades ejecutadas se encuentran a satisfacción y corresponden al objeto y "
        "obligaciones del contrato. Se recomienda autorizar el pago correspondiente al período."
    )

    doc.add_paragraph()
    doc.add_paragraph()
    doc.add_paragraph("_______________________________")
    doc.add_paragraph(contrato.supervisor_nombre or "—")
    doc.add_paragraph("Supervisor del contrato")

    buf = io.BytesIO()
    doc.save(buf)

    filename = (
        f"informe-supervision-{contrato.numero_contrato}-"
        f"{cuenta.anio}-{cuenta.mes:02d}.docx"
    )
    await logger.ainfo(
        "informe_supervision_generado",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        size=len(buf.getvalue()),
    )
    return buf.getvalue(), filename


def _safe_dirname(text: str, max_len: int = 80) -> str:
    """Sanitize text to a filesystem-safe folder name."""
    bad = '<>:"/\\|?*\n\r\t'
    cleaned = "".join(c if c not in bad else "-" for c in text).strip()
    cleaned = " ".join(cleaned.split())  # collapse whitespace
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned or "obligacion"


async def generar_zip_evidencias(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> tuple[bytes, str]:
    """Build a ZIP with one folder per obligation containing a README.txt placeholder.

    The structure helps the contractor organize physical evidence files by
    obligation. Each folder holds:
      - README.txt (description of obligation + activities + checklist)
    """
    cuenta, contrato, _usuario, obligaciones, actividades = await _load_context(
        db, usuario_id, cuenta_id
    )

    actividades_por_ob: dict[uuid.UUID | None, list[Actividad]] = {}
    for act in actividades:
        actividades_por_ob.setdefault(act.obligacion_id, []).append(act)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Root README
        root_readme = (
            f"Carpeta de evidencias\n"
            f"=====================\n\n"
            f"Contrato: {contrato.numero_contrato}\n"
            f"Objeto: {contrato.objeto}\n"
            f"Período: {_periodo_str(cuenta)}\n"
            f"Obligaciones: {len(obligaciones)}\n"
            f"Actividades reportadas: {len(actividades)}\n\n"
            f"Estructura:\n"
            f"  - Una subcarpeta por obligación contractual.\n"
            f"  - Cada subcarpeta contiene un README.txt con las actividades\n"
            f"    asociadas y el listado de evidencias esperadas.\n"
            f"  - Coloque dentro de cada subcarpeta los archivos de soporte\n"
            f"    (PDF, fotos, capturas, correos exportados, etc.).\n"
        )
        zf.writestr("LEEME.txt", root_readme)

        if not obligaciones:
            zf.writestr(
                "00_sin_obligaciones/LEEME.txt",
                "El contrato no tiene obligaciones registradas. "
                "Cargue el contrato y extraiga obligaciones primero.\n",
            )

        for idx, ob in enumerate(obligaciones, start=1):
            folder = f"{idx:02d}_{_safe_dirname(ob.descripcion)}"
            acts = actividades_por_ob.get(ob.id, [])
            lines = [
                f"Obligación #{idx} ({ob.tipo.value if ob.tipo else 'general'})",
                "=" * 60,
                "",
                ob.descripcion,
                "",
                f"Período: {_periodo_str(cuenta)}",
                "",
                "Actividades reportadas en este período:",
                "-" * 40,
            ]
            if acts:
                for j, act in enumerate(acts, start=1):
                    lines.append(f"{j}. {act.descripcion}")
                    if act.justificacion:
                        lines.append(f"   Justificación: {act.justificacion}")
                    lines.append(f"   Fecha: {_formato_fecha(act.fecha_realizacion)}")
                    if act.evidencias:
                        lines.append(f"   Evidencias adjuntas: {len(act.evidencias)}")
                        for ev in act.evidencias:
                            lines.append(f"     - {ev.nombre_archivo}")
                    lines.append("")
            else:
                lines.append("(sin actividades reportadas)")
                lines.append("")
            lines += [
                "Coloque aquí los archivos de soporte (correos, capturas, PDFs,",
                "fotografías, etc.) que evidencien el cumplimiento de esta obligación.",
            ]
            zf.writestr(f"{folder}/LEEME.txt", "\n".join(lines))

        # Activities not linked to any obligation
        sueltas = actividades_por_ob.get(None, [])
        if sueltas:
            lines = [
                "Actividades sin obligación vinculada",
                "=" * 60,
                "",
            ]
            for j, act in enumerate(sueltas, start=1):
                lines.append(f"{j}. {act.descripcion}")
                lines.append(f"   Fecha: {_formato_fecha(act.fecha_realizacion)}")
                lines.append("")
            zf.writestr("99_otras_actividades/LEEME.txt", "\n".join(lines))

    filename = (
        f"evidencias-{contrato.numero_contrato}-"
        f"{cuenta.anio}-{cuenta.mes:02d}.zip"
    )
    await logger.ainfo(
        "zip_evidencias_generado",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        size=len(buf.getvalue()),
    )
    return buf.getvalue(), filename
