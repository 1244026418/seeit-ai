"""Create the SeeIt AI schema.

Revision ID: 20260713_01
Revises:
Create Date: 2026-07-13 20:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260713_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("nickname", sa.String(length=50), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "media_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("upload_time", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "content_hash", name="uq_media_user_content_hash"),
    )
    op.create_index("ix_media_files_content_hash", "media_files", ["content_hash"], unique=False)
    op.create_index("ix_media_files_user_id", "media_files", ["user_id"], unique=False)
    op.create_index("ix_media_user_upload_time", "media_files", ["user_id", "upload_time"], unique=False)

    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_sessions_user_id", "upload_sessions", ["user_id"], unique=False)

    op.create_table(
        "analysis_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("goal", sa.String(length=500), nullable=False),
        sa.Column("goal_hash", sa.String(length=64), nullable=False),
        sa.Column("active_key", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("plan_json", sa.Text(), nullable=True),
        sa.Column("trace_json", sa.Text(), nullable=True),
        sa.Column("evaluation_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["media_id"], ["media_files.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_analysis_tasks_active_key"),
    )
    op.create_index("ix_analysis_tasks_media_id", "analysis_tasks", ["media_id"], unique=False)
    op.create_index("ix_analysis_tasks_user_id", "analysis_tasks", ["user_id"], unique=False)
    op.create_index("ix_task_media_goal_created", "analysis_tasks", ["media_id", "goal_hash", "created_at"], unique=False)
    op.create_index("ix_task_state_updated", "analysis_tasks", ["state", "updated_at"], unique=False)

    op.create_table(
        "evidence_segments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["media_id"], ["media_files.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_segments_media_id", "evidence_segments", ["media_id"], unique=False)
    op.create_index("ix_evidence_media_timeline", "evidence_segments", ["media_id", "start_ms"], unique=False)

    op.create_table(
        "analysis_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "user_id", name="uq_feedback_task_user"),
    )
    op.create_index("ix_analysis_feedback_task_id", "analysis_feedback", ["task_id"], unique=False)
    op.create_index("ix_analysis_feedback_user_id", "analysis_feedback", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("analysis_feedback")
    op.drop_table("evidence_segments")
    op.drop_table("analysis_tasks")
    op.drop_table("upload_sessions")
    op.drop_table("media_files")
    op.drop_table("users")
