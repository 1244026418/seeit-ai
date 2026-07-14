"""Add Bilibili import tasks and media source metadata.

Revision ID: 20260715_02
Revises: 20260713_01
Create Date: 2026-07-15 00:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_02"
down_revision: Union[str, None] = "20260713_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_files",
        sa.Column("source_type", sa.String(length=20), server_default="UPLOAD", nullable=False),
    )
    op.add_column("media_files", sa.Column("source_ref", sa.String(length=64), nullable=True))
    op.add_column("media_files", sa.Column("cover_url", sa.String(length=1024), nullable=True))

    op.create_table(
        "media_import_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("bvid", sa.String(length=12), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("active_key", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["media_id"], ["media_files.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_media_import_tasks_active_key"),
    )
    op.create_index("ix_media_import_tasks_user_id", "media_import_tasks", ["user_id"], unique=False)
    op.create_index("ix_media_import_tasks_media_id", "media_import_tasks", ["media_id"], unique=False)
    op.create_index("ix_media_import_tasks_bvid", "media_import_tasks", ["bvid"], unique=False)
    op.create_index("ix_import_task_state_updated", "media_import_tasks", ["state", "updated_at"], unique=False)


def downgrade() -> None:
    op.drop_table("media_import_tasks")
    op.drop_column("media_files", "cover_url")
    op.drop_column("media_files", "source_ref")
    op.drop_column("media_files", "source_type")
