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
    favorite_models: list[str] = []
    """Model ids the picker pins at the top. Set as JSON: `AGENTSPACE_FAVORITE_MODELS='["a/b", "c/d"]'`."""
    default_system_prompt: str = (
        "You are a coding assistant. Help the user inspect files, run commands and change code in their workspace."
    )
    """The preamble of a new session's system prompt; tool, rules and workspace sections are added on each run."""
    skills_dir: Path | None = None
    """Skill folders (each with a `SKILL.md`) shared by every session, read-only to the model (ADR-016).
    Defaults to `skills` in `data_dir`."""
    max_turns: int = 50
    max_output_tokens: int = 8192
    """Per model reply. Sent as `max_tokens`; raise it for long file writes."""
    compaction_enabled: bool = True
    """Summarize the older part of a session when its context nears the model's window (ADR-009)."""
    compaction_reserve_tokens: int = 16384
    """Compact once the context is within this many tokens of the window, leaving room for the next reply."""
    compaction_keep_recent_tokens: int = 20000
    """About how many tokens of the newest messages compaction keeps as they are."""
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
    def resolved_skills_dir(self) -> Path:
        """`skills_dir` if set, else the `skills` folder in `data_dir`."""
        return self.skills_dir or self.data_dir / "skills"

    @property
    def workspaces_dir(self) -> Path:
        """Parent folder of every session's workspace."""
        return self.data_dir / "workspaces"


# TS: @lru_cache memoizes the function, so every call returns the same Settings object (a lazy singleton).
@lru_cache
def get_settings() -> Settings:
    """The settings, read once per process."""
    return Settings()
