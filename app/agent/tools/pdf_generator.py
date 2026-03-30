"""PDF generator tool — HTML/Jinja2 → PDF via WeasyPrint."""

from __future__ import annotations

from jinja2 import BaseLoader, Environment


def generate_pdf_from_html(html_content: str) -> bytes:
    """Render HTML string to PDF bytes using WeasyPrint."""
    from weasyprint import HTML

    return HTML(string=html_content).write_pdf()


def generate_pdf_from_template(template_html: str, data: dict[str, str | int | float | None]) -> bytes:
    """Fill a Jinja2 HTML template and render to PDF."""
    env = Environment(loader=BaseLoader(), autoescape=True)
    tmpl = env.from_string(template_html)
    html = tmpl.render(**data)
    return generate_pdf_from_html(html)
