"""App settings, read from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentspace.tools import ToolSetName


# TS: like parsing process.env with a zod schema (e.g. t3-env): fields are filled from env vars and validated.
class Settings(BaseSettings):
    """Read from environment variables (prefix `AGENTSPACE_`) and `apps/api/.env`."""

    model_config = SettingsConfigDict(env_prefix="AGENTSPACE_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    """Holds each session's workspace folder, the skills and the generated secret key."""
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:55322/postgres"
    """A Postgres URL (ADR-027). The default is the shared dev database, `postgres` on the local Supabase that
    `pnpm dev:api` starts; a worktree that ran `pnpm db:own` sets its own in `.worktree.env`."""
    database_pool_size: int = Field(default=5, ge=1)
    """Connections kept open per API process; up to as many more open when it's busy."""
    database_statement_timeout_ms: int = Field(default=25_000, ge=1)
    """The longest one SQL statement may run before Postgres cancels it."""
    redis_url: str = "redis://127.0.0.1:55379/0"
    """Redis for run events and stop/steer requests, shared by every API server (ADR-029). The default is the
    local one `pnpm dev:api` starts; production points at Upstash (`rediss://`, ADR-030)."""
    redis_prefix: str = "agentspace:"
    """Starts every Redis key, so tests (or two apps) can share one Redis without seeing each other's keys."""
    run_heartbeat_seconds: float = Field(default=5, gt=0)
    """How often a server checks in for its runs and looks for runs whose server went silent."""
    run_stale_seconds: float = Field(default=30, gt=0)
    """How long a run's server may stay silent before the run is marked interrupted."""
    shutdown_grace_seconds: float = Field(default=0, ge=0)
    """On shutdown (a deploy's SIGTERM), how long running runs may go on to finish before the rest are interrupted.
    0 locally, so a reload is instant; production sets it below ECS's `stopTimeout` (ADR-030)."""

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
    """Give sessions the `bash` tool with the local backend. Off by default: commands run unsandboxed as the API's
    user (ADR-003). Sandboxed sessions always have it (ADR-035)."""

    workspace_backend: Literal["local", "daytona"] = "local"
    """Where sessions' files and commands live: a folder on this server, or a Daytona sandbox per session (ADR-035)."""
    daytona_api_key: str | None = Field(default=None, validation_alias="DAYTONA_API_KEY")
    """Required by the `daytona` backend; from the Daytona dashboard (Keys)."""
    sandbox_prefix: str = "agentspace"
    """Starts every sandbox's name (`<prefix>-<session id>`), so dev and production sandboxes are told apart."""
    sandbox_exec_timeout_seconds: float = Field(default=600, gt=0)
    """The longest a sandboxed command may run when the model gives no timeout."""
    sandbox_auto_stop_minutes: int = Field(default=15, ge=1)
    """Daytona stops a sandbox after this long without API calls; its files are kept."""
    sandbox_auto_archive_minutes: int = Field(default=1440, ge=1)
    """Daytona moves a stopped sandbox to cheaper storage after this long; it wakes a little slower."""
    sandbox_network: Literal["vendor_default", "allow_list", "blocked"] = "vendor_default"
    """A sandbox's outbound network (ADR-035). `vendor_default`: Daytona's own policy, which on its Tiers 1–2 is
    package registries, git hosts and model providers only (and can't be changed). `allow_list`: only
    `sandbox_allowed_domains` (Daytona Tier 3+, where the default is the whole internet). `blocked`: none."""
    sandbox_allowed_domains: list[str] = [
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "github.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
    ]
    """With `sandbox_network=allow_list`, the only sites a sandbox can reach. Set as JSON."""
    enable_mcp: bool = False
    """Start the saved MCP servers. Off by default: each one runs a command on the host (ADR-017)."""
    mcp_admin_token: str | None = None
    """Secret the web page must send to add, change, delete or test MCP servers; those routes refuse all while unset."""
    mcp_wait_seconds: float = 10
    """How long a run waits for its MCP servers to connect before going on without the slow ones."""
    mcp_call_timeout_seconds: float = 60
    """How long one MCP tool call may take."""
    mcp_idle_seconds: float = 600
    """Stop a session's MCP servers after this long without use."""
    supabase_url: str | None = None
    """The Supabase project (`https://<ref>.supabase.co`) whose sign-in tokens the API accepts (ADR-024).
    Unset = local mode: no login, anyone who reaches the API can use it."""
    secret_key: str | None = Field(default=None, min_length=32)
    """Encrypts users' MCP env values in the database (ADR-025); make one with `openssl rand -base64 32`.
    Unset = one is generated into `<data_dir>/secret.key`, next to the database it protects (fine locally only)."""
    auto_migrate: bool = True
    """Apply pending Alembic migrations when the app starts."""

    max_runs_per_user: int = Field(default=3, ge=1)
    """How many of one user's sessions may run at once (ADR-026)."""

    openrouter_api_key: str | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    """Local mode only: copied into the local user's saved keys at startup if they have none (ADR-026).
    Runs never read it; every user, the local one too, pays with their own saved key."""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Langfuse tracing (ADR-018): off unless both keys are set; the keys come from the project's settings page.
    langfuse_public_key: str | None = Field(default=None, validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_base_url: str = Field(default="https://cloud.langfuse.com", validation_alias="LANGFUSE_BASE_URL")
    """The EU cloud by default; `https://us.cloud.langfuse.com` for the US one, or a self-hosted URL."""

    # TS: a model_validator runs after every field is parsed, like zod's `.superRefine` on the whole object.
    @model_validator(mode="after")
    def _daytona_needs_a_key(self) -> Self:
        """Fail at startup, not on the first prompt, when the sandbox backend has no key."""
        if self.workspace_backend == "daytona" and not self.daytona_api_key:
            raise ValueError("AGENTSPACE_WORKSPACE_BACKEND=daytona needs DAYTONA_API_KEY.")
        return self

    # TS: @property makes a getter: `settings.resolved_skills_dir`, read without parentheses.
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
