"""merge alembic heads

Revision ID: m9n0o1p2q3r4
Revises: b1c2d3e4f5a6, g2h3i4j5k6l7
Create Date: 2026-04-28

"""

from typing import Sequence, Union

from alembic import op


revision: str = "m9n0o1p2q3r4"
down_revision: Union[str, tuple[str, str], None] = ("b1c2d3e4f5a6", "g2h3i4j5k6l7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Merge point only; no schema changes.
    pass


def downgrade() -> None:
    # Merge point only; no schema changes.
    pass

