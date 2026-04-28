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
    # Một số môi trường dev có thể đã tạo bảng thủ công / chạy nửa chừng;
    # dùng DDL idempotent để tránh fail khi bảng đã tồn tại.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS tiktok_cookie_settings (
                id SERIAL PRIMARY KEY,
                cookie_json TEXT NOT NULL,
                expires_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
            );
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_tiktok_cookie_settings_id
            ON tiktok_cookie_settings (id);
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_tiktok_cookie_settings_id;"))
    conn.execute(sa.text("DROP TABLE IF EXISTS tiktok_cookie_settings;"))

