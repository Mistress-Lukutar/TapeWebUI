"""
File:   0005_file_kind.py
Brief:  Add files.kind/inner_count/manifest_sha for archive entries.
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.8.0
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the archive-entry columns (existing rows are plain files)."""
    op.add_column(
        "files",
        sa.Column("kind", sa.String(length=16), nullable=False,
                  server_default="file"),
    )
    op.add_column(
        "files",
        sa.Column("inner_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "files",
        sa.Column("manifest_sha", sa.String(length=64), nullable=False,
                  server_default=""),
    )


def downgrade() -> None:
    """Remove the archive-entry columns."""
    op.drop_column("files", "manifest_sha")
    op.drop_column("files", "inner_count")
    op.drop_column("files", "kind")
