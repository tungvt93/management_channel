"""add tiktok sync schedule settings

Revision ID: h8i9j0k1l2m3
Revises: m9n0o1p2q3r4
Create Date: 2026-05-02

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, None] = "m9n0o1p2q3r4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS tiktok_sync_schedule_settings (
                id SERIAL PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                hour INTEGER NOT NULL DEFAULT 7,
                minute INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tiktok_sync_schedule_settings_id ON tiktok_sync_schedule_settings (id);"
        )
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tiktok_sync_schedule_settings_id"), table_name="tiktok_sync_schedule_settings")
    op.drop_table("tiktok_sync_schedule_settings")
