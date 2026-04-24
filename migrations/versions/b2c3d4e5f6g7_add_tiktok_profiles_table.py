"""add tiktok_profiles table

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6g7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tiktok_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('followers_count', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tiktok_profiles_url'), 'tiktok_profiles', ['url'], unique=True)
    op.create_index(op.f('ix_tiktok_profiles_id'), 'tiktok_profiles', ['id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_tiktok_profiles_id'), table_name='tiktok_profiles')
    op.drop_index(op.f('ix_tiktok_profiles_url'), table_name='tiktok_profiles')
    op.drop_table('tiktok_profiles')
