"""add tiktok sync runs table

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-04-28

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotent: app startup may create tables via `Base.metadata.create_all()`.
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS tiktok_sync_runs (
                id SERIAL PRIMARY KEY,
                kind VARCHAR(32) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'running',
                started_at TIMESTAMPTZ DEFAULT now(),
                finished_at TIMESTAMPTZ,
                message TEXT
            );
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tiktok_sync_runs_id ON tiktok_sync_runs (id);"
        )
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tiktok_sync_runs_id"), table_name="tiktok_sync_runs")
    op.drop_table("tiktok_sync_runs")

