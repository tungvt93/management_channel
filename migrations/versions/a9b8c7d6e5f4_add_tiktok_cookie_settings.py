"""add tiktok cookie settings table

Revision ID: a9b8c7d6e5f4
Revises: f1a2b3c4d5e6
Create Date: 2026-04-28

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tiktok_cookie_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cookie_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tiktok_cookie_settings_id"), "tiktok_cookie_settings", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tiktok_cookie_settings_id"), table_name="tiktok_cookie_settings")
    op.drop_table("tiktok_cookie_settings")

