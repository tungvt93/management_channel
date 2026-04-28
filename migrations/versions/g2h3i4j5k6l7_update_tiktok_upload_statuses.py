"""update tiktok_profiles.upload_status values & default

Revision ID: g2h3i4j5k6l7
Revises: f1a2b3c4d5e6
Create Date: 2026-04-28

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g2h3i4j5k6l7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Map old statuses -> new statuses
    conn.execute(
        sa.text(
            """
            UPDATE tiktok_profiles
            SET upload_status = CASE upload_status
                WHEN 'pending' THEN 'cho_up'
                WHEN 'in_progress' THEN 'dang_up'
                WHEN 'uploaded' THEN 'da_bat'
                ELSE upload_status
            END
            WHERE upload_status IN ('pending', 'in_progress', 'uploaded');
            """
        )
    )

    # Ensure any empty/null becomes default
    conn.execute(
        sa.text(
            """
            UPDATE tiktok_profiles
            SET upload_status = 'cho_up'
            WHERE upload_status IS NULL OR upload_status = '';
            """
        )
    )

    # Update server default to new value
    conn.execute(
        sa.text(
            """
            ALTER TABLE tiktok_profiles
            ALTER COLUMN upload_status SET DEFAULT 'cho_up';
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Best-effort reverse mapping:
    # - da_bat -> uploaded
    # - da_ban -> uploaded (không có tương đương cũ)
    conn.execute(
        sa.text(
            """
            UPDATE tiktok_profiles
            SET upload_status = CASE upload_status
                WHEN 'cho_up' THEN 'pending'
                WHEN 'dang_up' THEN 'in_progress'
                WHEN 'da_bat' THEN 'uploaded'
                WHEN 'da_ban' THEN 'uploaded'
                ELSE upload_status
            END
            WHERE upload_status IN ('cho_up', 'dang_up', 'da_bat', 'da_ban');
            """
        )
    )

    conn.execute(
        sa.text(
            """
            ALTER TABLE tiktok_profiles
            ALTER COLUMN upload_status SET DEFAULT 'pending';
            """
        )
    )

