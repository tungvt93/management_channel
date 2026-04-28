"""add tiktok_profiles.manager

Revision ID: e8f2a1c0b9d3
Revises: d7a1c9b3e2f4
Create Date: 2026-04-28

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f2a1c0b9d3"
down_revision: Union[str, None] = "d7a1c9b3e2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Reuse existing enum type channelmanager (create if missing for safety).
    conn.execute(
        sa.text(
            """
            DO $$ BEGIN
                CREATE TYPE channelmanager AS ENUM ('tung', 'long');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
            """
        )
    )

    conn.execute(
        sa.text(
            """
            ALTER TABLE tiktok_profiles
            ADD COLUMN IF NOT EXISTS manager channelmanager;
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE tiktok_profiles DROP COLUMN IF EXISTS manager;"))

