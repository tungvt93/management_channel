"""add tiktok_profiles.last_video_published_at

Revision ID: n1a2b3c4d5e6
Revises: m9n0o1p2q3r4
Create Date: 2026-04-29

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n1a2b3c4d5e6"
down_revision: Union[str, None] = "m9n0o1p2q3r4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tiktok_profiles",
        sa.Column("last_video_published_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tiktok_profiles", "last_video_published_at")

