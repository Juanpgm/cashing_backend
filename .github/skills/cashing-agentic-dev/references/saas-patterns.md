# SaaS Patterns — Créditos, Multi-tenancy, Suscripciones

## Multi-tenancy — Regla Absoluta

Todo acceso a datos de usuario DEBE tener el doble filtro: query + ownership check.

### En la query (SQLAlchemy)
```python
# app/services/mi_service.py
async def get_mis_recursos(
    db: AsyncSession, usuario_id: UUID
) -> list[MiModelo]:
    stmt = (
        select(MiModelo)
        .where(
            MiModelo.usuario_id == usuario_id,  # 1. Filtro de tenant
            MiModelo.deleted_at.is_(None),      # 2. Soft delete
        )
        .order_by(MiModelo.created_at.desc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()
```

### En el service (ownership check)
```python
async def get_recurso_o_404(
    db: AsyncSession, recurso_id: UUID, usuario_id: UUID
) -> MiModelo:
    stmt = select(MiModelo).where(MiModelo.id == recurso_id)
    result = await db.execute(stmt)
    recurso = result.scalar_one_or_none()

    if recurso is None:
        raise NotFoundError("MiModelo", recurso_id)

    # Verificar ownership explícitamente
    if recurso.usuario_id != usuario_id:
        raise ForbiddenError("MiModelo", recurso_id)

    return recurso
```

## Sistema de Créditos

### Costos por operación (en app/core/config.py)
```python
class Settings(BaseSettings):
    CREDITS_PER_CHAT_MESSAGE: int = 1
    CREDITS_PER_PIPELINE_RUN: int = 5        # Procesa contrato completo
    CREDITS_PER_EVIDENCE_RUN: int = 3        # Gmail search + matching
    CREDITS_PER_ACTIVITIES_GENERATION: int = 2
    CREDITS_PER_DOCUMENT_GENERATION: int = 5  # PDF + DOCX final
    CREDITS_PER_EXTRACTION_RUN: int = 3       # Extrae obligaciones
```

### Dependency de FastAPI (verificar antes de ejecutar)
```python
# app/api/deps.py
def require_credits(amount: int):
    async def _check(
        current_user: Usuario = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        creditos = await CreditoService.get_creditos_disponibles(db, current_user.id)
        if creditos < amount:
            raise InsufficientCreditsError(required=amount, available=creditos)
        return creditos
    return _check

# En el endpoint
@router.post("/agent/run")
async def run_agent(
    _=Depends(require_credits(settings.CREDITS_PER_PIPELINE_RUN)),
    current_user: Usuario = Depends(get_current_user),
    ...
):
    ...
```

### Consumir créditos (SOLO al completar, no en error)
```python
# app/services/credito_service.py
async def consume(
    db: AsyncSession,
    usuario_id: UUID,
    cantidad: int,
    descripcion: str,
) -> Credito:
    """
    Descuenta créditos. Llamar SOLO cuando la operación fue exitosa.
    Los errores del agente no consumen créditos.
    """
    credito = await get_credito_activo(db, usuario_id)
    if credito.disponibles < cantidad:
        raise InsufficientCreditsError(cantidad, credito.disponibles)
    
    credito.disponibles -= cantidad
    credito.consumidos += cantidad
    
    # Auditoría
    audit_entry = AuditLog(
        usuario_id=usuario_id,
        accion="credito_consumido",
        detalle={"cantidad": cantidad, "descripcion": descripcion},
    )
    db.add(audit_entry)
    await db.commit()
    return credito
```

## Planes de Suscripción

```python
class PlanSuscripcion(str, Enum):
    FREE = "FREE"           # 20 créditos/mes
    PRO = "PRO"             # 200 créditos/mes
    ENTERPRISE = "ENTERPRISE"  # Ilimitado

CREDITOS_POR_PLAN = {
    PlanSuscripcion.FREE: 20,
    PlanSuscripcion.PRO: 200,
    PlanSuscripcion.ENTERPRISE: None,  # None = sin límite
}

FEATURES_POR_PLAN = {
    PlanSuscripcion.FREE: {"chat", "pipeline"},
    PlanSuscripcion.PRO: {"chat", "pipeline", "evidence", "drive", "calendar"},
    PlanSuscripcion.ENTERPRISE: {"*"},  # Todas las features
}
```

### Verificar feature access
```python
def require_plan_feature(feature: str):
    async def _check(current_user: Usuario = Depends(get_current_user)):
        plan = current_user.suscripcion_activa.plan if current_user.suscripcion_activa else PlanSuscripcion.FREE
        features = FEATURES_POR_PLAN[plan]
        if "*" not in features and feature not in features:
            raise PlanUpgradeRequiredError(feature=feature, required_plan="PRO")
    return _check

# Uso
@router.post("/agent/evidence-run")
async def run_evidence(
    _=Depends(require_plan_feature("evidence")),
    __=Depends(require_credits(settings.CREDITS_PER_EVIDENCE_RUN)),
    ...
):
```

## Webhooks de Wompi (Pagos)

```python
# app/api/v1/pagos.py
@router.post("/wompi/webhook")
async def wompi_webhook(
    payload: WompiWebhookPayload,
    signature: str = Header(..., alias="X-Event-Signature"),
    db: AsyncSession = Depends(get_db),
):
    # 1. Verificar firma HMAC del webhook
    if not verify_wompi_signature(payload.raw, signature, settings.WOMPI_EVENTS_SECRET):
        raise HTTPException(400, "Invalid signature")
    
    # 2. Procesar evento
    if payload.event == "transaction.updated" and payload.data.transaction.status == "APPROVED":
        await SuscripcionService.activar_o_renovar(db, payload)
        await CreditoService.recargar_mensual(db, usuario_id, plan)
```

## API Keys Enterprise

```python
# Generar API key Enterprise
import secrets

def generate_api_key() -> tuple[str, str]:
    """Retorna (key_plain, key_hashed). Solo guardar el hash."""
    raw = secrets.token_urlsafe(32)
    key = f"cashin_live_{raw}"
    hashed = bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()
    return key, hashed

# Validar en requests
async def get_current_user_or_api_key(
    authorization: str | None = Header(None),
    x_cashin_api_key: str | None = Header(None, alias="X-CashIn-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    if x_cashin_api_key:
        user = await ApiKeyService.validate(db, x_cashin_api_key)
        if not user:
            raise HTTPException(401, "Invalid API key")
        return user
    # Fallback a JWT normal
    return await get_current_user(authorization, db)
```

## Convenciones de Modelos ORM

```python
# Siempre heredar los tres mixins
class MiModelo(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "mi_tabla"   # plural + español + snake_case

# Soft delete — NUNCA DELETE físico en producción
async def soft_delete(db: AsyncSession, modelo_id: UUID, usuario_id: UUID):
    await db.execute(
        update(MiModelo)
        .where(MiModelo.id == modelo_id, MiModelo.usuario_id == usuario_id)
        .values(deleted_at=datetime.utcnow())
    )
    await db.commit()

# Nombres de tabla: plural + español + snake_case
__tablename__ = "cuentas_cobro"   # ✅
__tablename__ = "CuentaCobro"     # ❌ PascalCase
__tablename__ = "billing_period"  # ❌ inglés
__tablename__ = "cuenta_cobro"    # ❌ singular
```

## Migraciones Alembic

```bash
# Crear nueva migración
make migrate-create MSG="descripcion_corta_del_cambio"
# Editar el archivo generado en alembic/versions/

# Aplicar
make migrate

# Revertir
make migrate-down

# Ver estado actual
make migrate-status
```

### Plantilla de migración

```python
# alembic/versions/007_mi_nueva_tabla.py
"""Descripción corta del cambio

Revision ID: xxx
Revises: yyy
Create Date: ...
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"

def upgrade():
    op.create_table(
        "mi_tabla",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("campo", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mi_tabla_usuario_id", "mi_tabla", ["usuario_id"])

def downgrade():
    op.drop_index("ix_mi_tabla_usuario_id")
    op.drop_table("mi_tabla")
```
