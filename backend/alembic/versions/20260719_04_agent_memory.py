"""Add persisted Agent conversation memory.

Revision ID: 20260719_04
Revises: 20260716_03
Create Date: 2026-07-19 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260719_04"
down_revision: Union[str, None] = "20260716_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=False),
        sa.Column("goal", sa.String(length=500), nullable=False),
        sa.Column("goal_hash", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["media_id"], ["media_files.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "media_id", "goal_hash", name="uq_agent_session_scope"),
    )
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"])
    op.create_index("ix_agent_sessions_media_id", "agent_sessions", ["media_id"])
    op.create_index("ix_agent_sessions_goal_hash", "agent_sessions", ["goal_hash"])
    op.create_index("ix_agent_session_user_updated", "agent_sessions", ["user_id", "updated_at"])

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_messages_session_id", "agent_messages", ["session_id"])
    op.create_index("ix_agent_messages_user_id", "agent_messages", ["user_id"])
    op.create_index("ix_agent_message_session_created", "agent_messages", ["session_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_message_session_created", table_name="agent_messages")
    op.drop_index("ix_agent_messages_user_id", table_name="agent_messages")
    op.drop_index("ix_agent_messages_session_id", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_index("ix_agent_session_user_updated", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_goal_hash", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_media_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
