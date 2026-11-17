"""add tools to sessions

Revision ID: 3220c661edc2
Revises: d4afb776bd7c
Create Date: 2026-11-17 16:01:24.850558

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3220c661edc2"
down_revision: str | Sequence[str] | None = "d4afb776bd7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tools every session had before this column existed. Spelled out, not imported from
# `agentspace.tools`, so this migration does the same thing however the presets change later.
EXISTING_SESSION_TOOLS = '["read", "bash", "edit", "write", "grep", "find", "ls"]'


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        # NOT NULL needs a value for rows that already exist; new rows always get one from the app.
        batch_op.add_column(
            sa.Column("tools", sa.JSON(), nullable=False, server_default=sa.text(f"'{EXISTING_SESSION_TOOLS}'"))
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("tools")
