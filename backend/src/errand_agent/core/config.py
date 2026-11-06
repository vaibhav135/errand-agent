"""Settings: typed configuration read once from the environment.

What: A `Settings` object (pydantic-settings) holding paths, model choice, mock
      seed and provider API keys. App settings use the `ERRAND_` prefix;
      provider keys use their standard names (`ANTHROPIC_API_KEY`, ...).
      A local `.env` (see `.env.example`) is honoured.

Why:  One validated place for configuration, so a bad value fails at startup
      rather than deep inside a booking, and tests can build isolated settings
      (tmp paths, fixed seed) without touching global state.

How:  `data_dir` is the root; `agent_db_path`, `mock_db_path` and `trace_dir`
      derive from it unless set explicitly. Tests construct
      `Settings(_env_file=None, ...)` so a developer's `.env` never leaks in.
      Limits and token budgets join here in the phases that enforce them.
      Provider keys are `SecretStr` fields (masked in reprs and logs) and are
      passed explicitly to the model provider. They are fields rather than
      plain env vars because pydantic-settings reads `.env` into Settings only;
      it does not export to `os.environ`, where provider SDKs would look.
"""

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Derived path fields -> file/dir name under data_dir.
_DERIVED = {
    "agent_db_path": "agent.db",
    "mock_db_path": "mock_world.db",
    "trace_dir": "traces",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ERRAND_",
        env_file=".env",
        extra="ignore",
        # `KEY=` (blank, as in .env.example) means "unset", not "" — otherwise an
        # empty API key or model string would look configured.
        env_ignore_empty=True,
    )

    data_dir: Path = Path("var")
    agent_db_path: Path
    mock_db_path: Path
    trace_dir: Path

    # PydanticAI model string, e.g. "anthropic:claude-sonnet-5-5". None until chosen;
    # the agent refuses to start without it rather than silently picking one.
    model: str | None = None
    # Model for the quarantined review reader; defaults to `model` (e.g. a cheaper Haiku).
    review_model: str | None = None
    mock_seed: int = 42

    # Hard per-run limits (one run = one inbound event handled end to end).
    max_model_turns: int = Field(default=8, ge=1)
    max_tool_calls: int = Field(default=20, ge=1)
    max_run_seconds: float = Field(default=60.0, gt=0)

    # Fixed model-context budget per turn (estimated tokens), and the memories' share.
    context_token_budget: int = Field(default=6000, ge=500)
    memory_token_budget: int = Field(default=600, ge=0)

    # Load the generated demo user (profile, 55 memories, 200 messages) into an empty DB.
    seed_demo_user: bool = True

    # Background reconciler cadence; 0 disables the loop (the startup pass still runs).
    reconcile_interval_seconds: float = Field(default=30.0, ge=0)

    # validation_alias = the standard, unprefixed env var name.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    google_api_key: SecretStr | None = Field(default=None, validation_alias="GOOGLE_API_KEY")

    @model_validator(mode="before")
    @classmethod
    def _derive_paths(cls, data: Any) -> Any:
        # "before" (not "after") so the derived fields can stay typed as plain
        # `Path` instead of `Path | None`; callers never have to narrow them.
        if isinstance(data, dict):
            root = Path(data.get("data_dir", "var"))
            for field, name in _DERIVED.items():
                data.setdefault(field, root / name)
        return data


def load_settings() -> Settings:
    """Settings from the environment / .env (what the running app uses)."""
    # The derived paths are filled by `_derive_paths` before validation, which a
    # static type checker can't see, so the no-argument call looks incomplete to it.
    return Settings()  # pyright: ignore[reportCallIssue]
