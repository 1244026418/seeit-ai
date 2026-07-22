"""Persist structured Agent final answers.

Revision ID: 20260721_06
Revises: 20260720_05
Create Date: 2026-07-21 15:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260721_06"
down_revision: Union[str, None] = "20260720_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("analysis_tasks", sa.Column("answerable", sa.Boolean(), nullable=True))
    op.add_column("analysis_tasks", sa.Column("final_answer", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_tasks", "final_answer")
    op.drop_column("analysis_tasks", "answerable")
