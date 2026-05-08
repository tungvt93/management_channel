"""merge heads n1a2b3c4d5e6 and h8i9j0k1l2m3

Revision ID: p3q4r5s6t7u8
Revises: n1a2b3c4d5e6, h8i9j0k1l2m3
Create Date: 2026-05-08

"""

from typing import Sequence, Union

from alembic import op

revision: str = "p3q4r5s6t7u8"
down_revision: Union[str, tuple[str, str], None] = ("n1a2b3c4d5e6", "h8i9j0k1l2m3")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
