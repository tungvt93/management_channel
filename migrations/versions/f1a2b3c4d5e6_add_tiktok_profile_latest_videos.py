"""add tiktok_profiles latest video views snapshot

Revision ID: f1a2b3c4d5e6
Revises: e8f2a1c0b9d3
Create Date: 2026-04-28

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e8f2a1c0b9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tiktok_profiles", sa.Column("latest_videos_json", sa.Text(), nullable=True))
    op.add_column("tiktok_profiles", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tiktok_profiles", "last_synced_at")
    op.drop_column("tiktok_profiles", "latest_videos_json")

