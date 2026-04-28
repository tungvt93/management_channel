"""add channels.manager

Revision ID: d7a1c9b3e2f4
Revises: c4d5e6f7g8h9
Create Date: 2026-04-28

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7a1c9b3e2f4"
down_revision: Union[str, None] = "c4d5e6f7g8h9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Create enum type if it doesn't exist (idempotent).
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

    # Add column if not exists (safe for existing DBs).
    conn.execute(
        sa.text(
            """
            ALTER TABLE channels
            ADD COLUMN IF NOT EXISTS manager channelmanager;
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE channels DROP COLUMN IF EXISTS manager;"))
    conn.execute(sa.text("DROP TYPE IF EXISTS channelmanager;"))

