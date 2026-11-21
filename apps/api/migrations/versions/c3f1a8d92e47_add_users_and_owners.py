"""add users, an owner on sessions and MCP servers, and encrypt MCP env

Revision ID: c3f1a8d92e47
Revises: b7e2a9c41d05
Create Date: 2026-11-21 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import context, op

from agentspace.config import get_settings
from agentspace.encryption import SecretBox, load_server_secret

# revision identifiers, used by Alembic.
revision: str = "c3f1a8d92e47"
down_revision: str | Sequence[str] | None = "b7e2a9c41d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied, not imported from the models: a migration must keep doing what it did when written.
LOCAL_USER_ID = "local"
# Names SQLite's unnamed `UNIQUE (name)` on mcp_servers, so the batch below can drop it.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}

mcp_servers = sa.table(
    "mcp_servers", sa.column("id", sa.String), sa.column("user_id", sa.String), sa.column("env", sa.JSON)
)


def secret_box() -> SecretBox:
    """The app's box when the app runs the migration, else one from the settings (the `alembic` CLI)."""
    box = context.config.attributes.get("secret_box")
    if box is None:
        settings = get_settings()
        box = SecretBox(load_server_secret(settings.secret_key, settings.data_dir))
    return box


def map_env(convert) -> None:  # noqa: ANN001  (a function str, str -> str)
    """Rewrite every MCP server's env values with `convert(user_id, value)`."""
    conn = op.get_bind()
    rows = conn.execute(sa.select(mcp_servers.c.id, mcp_servers.c.user_id, mcp_servers.c.env)).all()
    for server_id, user_id, env in rows:
        if env:
            new_env = {name: convert(user_id, value) for name, value in env.items()}
            conn.execute(sa.update(mcp_servers).where(mcp_servers.c.id == server_id).values(env=new_env))


def name_unique_constraint() -> str:
    """The name of `UNIQUE (name)` on mcp_servers: Postgres's own (`mcp_servers_name_key`), or NAMING's on SQLite."""
    # Postgres names an unnamed constraint itself; SQLite reflects it with no name at all.
    found = sa.inspect(op.get_bind()).get_unique_constraints("mcp_servers")
    return next((c["name"] for c in found if c["column_names"] == ["name"] and c["name"]), "uq_mcp_servers_name")


def upgrade() -> None:
    """Upgrade schema: users own their sessions and MCP servers, and MCP env is encrypted (ADR-025)."""
    op.create_table(
        "users",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("email", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Every row made before users existed goes to the local user, who is also local mode's only user.
    insert_local = sa.text("INSERT INTO users (id, created_at) VALUES (:id, CURRENT_TIMESTAMP)")
    op.execute(insert_local.bindparams(id=LOCAL_USER_ID))

    old_unique = name_unique_constraint()
    for table in ("sessions", "mcp_servers"):
        with op.batch_alter_table(table, naming_convention=NAMING) as batch_op:
            # The default fills existing rows; it is dropped below so new rows must name their owner.
            batch_op.add_column(
                sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=LOCAL_USER_ID)
            )
            batch_op.create_index(f"ix_{table}_user_id", ["user_id"])
            batch_op.create_foreign_key(f"fk_{table}_user_id_users", "users", ["user_id"], ["id"], ondelete="CASCADE")
            if table == "mcp_servers":
                # A name is unique per user now, not across everyone.
                batch_op.drop_constraint(old_unique, type_="unique")
                batch_op.create_unique_constraint("uq_mcp_servers_user_id_name", ["user_id", "name"])
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column("user_id", server_default=None)

    box = secret_box()
    map_env(box.encrypt)


def downgrade() -> None:
    """Downgrade schema: env back to plain text, then drop the owners and users."""
    box = secret_box()
    map_env(box.decrypt)
    for table in ("mcp_servers", "sessions"):
        with op.batch_alter_table(table) as batch_op:
            if table == "mcp_servers":
                batch_op.drop_constraint("uq_mcp_servers_user_id_name", type_="unique")
                batch_op.create_unique_constraint("uq_mcp_servers_name", ["name"])
            batch_op.drop_constraint(f"fk_{table}_user_id_users", type_="foreignkey")
            batch_op.drop_index(f"ix_{table}_user_id")
            batch_op.drop_column("user_id")
    op.drop_table("users")
