"""initial_schema

Revision ID: 215d5989b1cc
Revises: 
Create Date: 2026-04-23 00:13:24.602963

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '215d5989b1cc'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create full initial schema (idempotent — safe to run on existing DBs)."""
    conn = op.get_bind()

    # Create enums only if they don't exist
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE platform AS ENUM ('youtube', 'tiktok');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE videostatus AS ENUM ('available', 'holded', 'done');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE scrapingstatus AS ENUM ('idle', 'in_progress', 'success', 'failed');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    # Create channels table if it doesn't exist
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS channels (
            id SERIAL PRIMARY KEY,
            url VARCHAR NOT NULL UNIQUE,
            platform platform NOT NULL,
            name VARCHAR,
            scraping_status scrapingstatus NOT NULL DEFAULT 'idle',
            last_scraped_at TIMESTAMPTZ,
            scraping_error TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_channels_url ON channels (url);
    """))

    # Create video_links table if it doesn't exist
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS video_links (
            id SERIAL PRIMARY KEY,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            url VARCHAR NOT NULL UNIQUE,
            status videostatus NOT NULL DEFAULT 'available',
            upload_date TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_video_links_url ON video_links (url);
    """))

    # Add new columns to channels if they don't exist yet (for existing DBs)
    conn.execute(sa.text("""
        ALTER TABLE channels ADD COLUMN IF NOT EXISTS scraping_status scrapingstatus NOT NULL DEFAULT 'idle';
        ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ;
        ALTER TABLE channels ADD COLUMN IF NOT EXISTS scraping_error TEXT;
    """))



def downgrade() -> None:
    """Drop all tables and enums."""
    op.drop_table('video_links')
    op.drop_table('channels')

    sa.Enum(name='scrapingstatus').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='videostatus').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='platform').drop(op.get_bind(), checkfirst=True)
