"""Recorded end position for backup sessions.

Adds ``sessions.end_position`` — the tape-absolute next-free position
the engine observes after a session's write pass. Verify-damaged files
were written and occupy tape positions, so the next session's header
position must come from this record instead of an ok-only entry count
(which silently desynchronized every later session from the physical
layout after any readback mismatch).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("end_position", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "end_position")
