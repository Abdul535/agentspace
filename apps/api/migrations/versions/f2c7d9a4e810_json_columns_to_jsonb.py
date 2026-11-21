"""json columns to jsonb: Postgres stores them parsed (ADR-027)

Revision ID: f2c7d9a4e810
Revises: e5b8d3f17a20
Create Date: 2026-11-22 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2c7d9a4e810"
down_revision: str | Sequence[str] | None = "e5b8d3f17a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every JSON column so far, as (table, column).
COLUMNS = [("sessions", "tools"), ("messages", "data"), ("mcp_servers", "args"), ("mcp_servers", "env")]


def upgrade() -> None:
    """Upgrade schema."""
    for table, column in COLUMNS:
        op.alter_column(table, column, type_=postgresql.JSONB(), postgresql_using=f"{column}::jsonb")


def downgrade() -> None:
    """Downgrade schema."""
    for table, column in COLUMNS:
        op.alter_column(table, column, type_=postgresql.JSON(), postgresql_using=f"{column}::json")
