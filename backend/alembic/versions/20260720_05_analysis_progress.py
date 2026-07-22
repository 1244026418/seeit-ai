"""Add persisted analysis stage progress.

Revision ID: 20260720_05
Revises: 20260719_04
Create Date: 2026-07-20 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260720_05"
down_revision: Union[str, None] = "20260719_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("analysis_tasks", sa.Column("stage", sa.String(length=32), nullable=True))
    op.add_column(
        "analysis_tasks",
        sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "analysis_tasks",
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("analysis_tasks", sa.Column("progress_message", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_tasks", "progress_message")
    op.drop_column("analysis_tasks", "progress_total")
    op.drop_column("analysis_tasks", "progress_current")
    op.drop_column("analysis_tasks", "stage")
