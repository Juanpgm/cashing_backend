"""InviteCode — waitlist / invite-code gate for account creation.

When the waitlist gate is enabled (``settings.WAITLIST_ENABLED``), creating an
account (email registration or first-time Google sign-in) requires a valid,
active, non-exhausted invite code. Each successful signup consumes one use.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class InviteCode(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "invite_codes"

    codigo: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    max_usos: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    usos_actuales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    nota: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Optional human label, e.g. campaign or invitee name.",
    )

    @property
    def disponible(self) -> bool:
        """True when the code can still be used to create an account."""
        return self.activo and self.usos_actuales < self.max_usos
