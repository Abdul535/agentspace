"""add runs: who drives each session's run, so several API servers can share them (ADR-029)

Revision ID: 08f8cb127d20
Revises: f2c7d9a4e810
Create Date: 2026-11-22 05:06:00.488959

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "08f8cb127d20"
down_revision: str | Sequence[str] | None = "f2c7d9a4e810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "runs",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("owner", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("heartbeat_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("started_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("ended_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    running = sa.text("status = 'running'")
    op.create_index("runs_active_by_user", "runs", ["user_id"], postgresql_where=running)
    op.create_index("runs_one_active", "runs", ["session_id"], unique=True, postgresql_where=running)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("runs_one_active", table_name="runs")
    op.drop_index("runs_active_by_user", table_name="runs")
    op.drop_table("runs")
