"""Expand Agent trace storage for model tool-calling runs.

Revision ID: 20260716_03
Revises: 20260715_02
Create Date: 2026-07-16 02:15:00
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260716_03"
down_revision: Union[str, None] = "20260715_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        op.alter_column(
            "analysis_tasks",
            "trace_json",
            existing_type=mysql.TEXT(),
            type_=mysql.LONGTEXT(),
            existing_nullable=True,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        op.alter_column(
            "analysis_tasks",
            "trace_json",
            existing_type=mysql.LONGTEXT(),
            type_=mysql.TEXT(),
            existing_nullable=True,
        )
