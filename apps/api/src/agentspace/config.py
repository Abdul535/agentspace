"""App settings, read from the environment."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentspace.tools import ToolSetName


# TS: like parsing process.env with a zod schema (e.g. t3-env): fields are filled from env vars and validated.
class Settings(BaseSettings):
    """Read from environment variables (prefix `AGENTSPACE_`) and `apps/api/.env`."""

    model_config = SettingsConfigDict(env_prefix="AGENTSPACE_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    """Holds the SQLite database and each session's workspace folder."""
    database_url: str | None = None
    """Defaults to a SQLite file in `data_dir`."""

    default_provider: str = "openrouter"
    default_model: str = "anthropic/claude-sonnet-5.5"
    default_system_prompt: str = (
        "You are a helpful assistant working in a workspace folder. "
        "Use your tools to inspect and change files in it. "
        "Paths are relative to the workspace."
    )
    max_turns: int = 50
    max_output_tokens: int = 8192
    """Per model reply. Sent as `max_tokens`; raise it for long file writes."""
    default_tool_set: ToolSetName = "all"
    """Tool preset for sessions created without one: `all`, `coding` or `read_only`."""
    enable_bash: bool = False
    """Give sessions the `bash` tool. Off by default: commands run unsandboxed as the API's user (ADR-003)."""
    auto_migrate: bool = True
    """Apply pending Alembic migrations when the app starts."""

    openrouter_api_key: str | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # TS: @property makes a getter: `settings.resolved_database_url`, read without parentheses.
    @property
    def resolved_database_url(self) -> str:
        """`database_url` if set, else the SQLite file in `data_dir`."""
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir / 'agentspace.db'}"

    @property
    def workspaces_dir(self) -> Path:
        """Parent folder of every session's workspace."""
        return self.data_dir / "workspaces"


# TS: @lru_cache memoizes the function, so every call returns the same Settings object (a lazy singleton).
@lru_cache
def get_settings() -> Settings:
    """The settings, read once per process."""
    return Settings()
