"""Runs Alembic migrations from code, so the app can upgrade its database at startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config

# TS: path.resolve(__dirname, "../../.."); parents[0] is this file's folder.
API_ROOT = Path(__file__).resolve().parents[3]
"""`apps/api`, which holds `alembic.ini` and `migrations/`."""


def upgrade_to_head(database_url: str) -> None:
    """Apply every pending migration. Blocking; Alembic's env runs its own event loop."""
    config = Config(API_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
