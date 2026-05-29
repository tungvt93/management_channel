"""add channel_id to youtube_channels

Revision ID: 4b5c6d7e8f9a
Revises: 3a4b5c6d7e8f
Create Date: 2026-05-29

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4b5c6d7e8f9a"
down_revision: Union[str, tuple[str, ...], None] = "3a4b5c6d7e8f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "youtube_channels",
        sa.Column("channel_id", sa.String(), nullable=True)
    )
    op.create_index(op.f("ix_youtube_channels_channel_id"), "youtube_channels", ["channel_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_youtube_channels_channel_id"), table_name="youtube_channels")
    op.drop_column("youtube_channels", "channel_id")
