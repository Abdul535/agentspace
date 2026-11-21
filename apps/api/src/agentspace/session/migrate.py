"""Runs Alembic migrations from code, so the app can upgrade its database at startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from agentspace.encryption import SecretBox

# TS: path.resolve(__dirname, "../../.."); parents[0] is this file's folder.
API_ROOT = Path(__file__).resolve().parents[3]
"""`apps/api`, which holds `alembic.ini` and `migrations/`."""


def upgrade_to_head(database_url: str, secret_box: SecretBox | None = None) -> None:
    """Apply every pending migration. Blocking; Alembic's env runs its own event loop.

    `secret_box` is the app's, for migrations that encrypt data; without it they build one from the settings.
    """
    config = Config(API_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["configure_logger"] = False
    config.attributes["secret_box"] = secret_box
    command.upgrade(config, "head")
