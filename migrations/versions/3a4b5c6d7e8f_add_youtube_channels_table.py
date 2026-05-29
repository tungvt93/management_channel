"""add youtube_channels table

Revision ID: 3a4b5c6d7e8f
Revises: p3q4r5s6t7u8
Create Date: 2026-05-29

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3a4b5c6d7e8f"
down_revision: Union[str, tuple[str, ...], None] = "p3q4r5s6t7u8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "youtube_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("last_video_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index(op.f("ix_youtube_channels_id"), "youtube_channels", ["id"], unique=False)
    op.create_index(op.f("ix_youtube_channels_url"), "youtube_channels", ["url"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_youtube_channels_url"), table_name="youtube_channels")
    op.drop_index(op.f("ix_youtube_channels_id"), table_name="youtube_channels")
    op.drop_table("youtube_channels")
