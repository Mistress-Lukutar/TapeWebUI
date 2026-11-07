"""Per-tape verification timestamp for catalog sync.

Adds ``tapes.last_verified_at`` — the last time a quick or deep verify
matched the physical cartridge (by reading its t0 header) to this
catalog entry. NULL means the tape has never been verified. The column
is cleared when the tape is marked erased.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tapes",
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tapes", "last_verified_at")
