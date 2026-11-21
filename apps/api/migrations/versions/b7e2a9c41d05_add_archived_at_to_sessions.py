"""add archived_at to sessions

Revision ID: b7e2a9c41d05
Revises: 99114cf0b8db
Create Date: 2026-11-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2a9c41d05"
down_revision: str | Sequence[str] | None = "99114cf0b8db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        # Null means not archived, so every existing session stays in the list.
        batch_op.add_column(sa.Column("archived_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("archived_at")
