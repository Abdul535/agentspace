"""add thinking level to sessions

Revision ID: 47f53c9e80cf
Revises: 3220c661edc2
Create Date: 2026-11-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "47f53c9e80cf"
down_revision: str | Sequence[str] | None = "3220c661edc2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        # Existing sessions never asked for thinking, so they keep the model's default (ADR-012).
        batch_op.add_column(sa.Column("thinking_level", sa.String(), nullable=False, server_default="off"))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("thinking_level")
