"""
File:   0004_session_kind.py
Brief:  Add sessions.kind (full | incremental) + job/started index.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the session kind column and a job-name lookup index."""
    op.add_column(
        "sessions",
        sa.Column("kind", sa.String(length=16), nullable=False,
                  server_default="full"),
    )
    op.create_index(
        "ix_sessions_job_started", "sessions", ["job_name", "started_at"]
    )


def downgrade() -> None:
    """Remove the kind column and the job-name lookup index."""
    op.drop_index("ix_sessions_job_started", table_name="sessions")
    op.drop_column("sessions", "kind")
