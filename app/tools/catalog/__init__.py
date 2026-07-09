"""Tool catalog — importing this package registers every catalog tool.

Entrypoint: `import app.tools.catalog` (module import, not the individual
submodules) guarantees every `@tool`-decorated handler below has run and
populated `app.tools.registry.TOOL_REGISTRY`. Callers (the MCP server, tests,
etc.) should rely on this side effect rather than importing catalog submodules
directly.

Deliberately NOT registered here (and never should be): authentication, user
registration, payments/credits management, and any other user-admin
capability. Those stay behind the normal authenticated API — tools only wrap
read/write operations on cuentas de cobro, contratos, checklist, informes, and
evidence.
"""

from app.tools.catalog import (
    checklist,
    cuentas,
    evidencias,
    importar_documento,
    informes,
    listar_contratos,
    listar_cuentas_cobro,
    secop,
)

__all__ = [
    "checklist",
    "cuentas",
    "evidencias",
    "importar_documento",
    "informes",
    "listar_contratos",
    "listar_cuentas_cobro",
    "secop",
]
