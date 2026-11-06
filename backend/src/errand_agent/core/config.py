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
      Models default to Sonnet (orchestrator) and Haiku (review reader), so one
      Anthropic key is enough to start; `default_model_notice()` says when defaults
      are in use, so the choice is never silent. An explicitly set ERRAND_MODEL is
      followed by the review reader too, so one key of any provider is enough.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The cheaper good ones: Sonnet plans multi-step tool use well at half Opus's price;
# Haiku only picks fixed labels from reviews, which a small model does well.
DEFAULT_MODEL = "anthropic:claude-sonnet-5-5"
DEFAULT_REVIEW_MODEL = "anthropic:claude-haiku-5-5"

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

    # PydanticAI model string ("<provider>:<model>"); see DEFAULT_MODEL.
    model: str = DEFAULT_MODEL
    # The quarantined review reader's model; use `review_model_spec` for the resolved one.
    review_model: str | None = None
    # Anthropic models only (D5). Effort sets how much Claude thinks (Sonnet 5.5 thinks
    # adaptively by default at "high"; it can't be switched off); None = the API's
    # default. prompt_cache caches the stable prefix (tool definitions, instructions).
    # Defaults measured with `make latency`: low effort halved errand turns, caching
    # cut cost 55-83%, same steps and outcomes.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    prompt_cache: bool = True
    mock_seed: int = 42

    # Hard per-run limits (one run = one inbound event handled end to end).
    max_model_turns: int = Field(default=8, ge=1)
    max_tool_calls: int = Field(default=20, ge=1)
    max_run_seconds: float = Field(default=60.0, gt=0)

    # Fixed model-context budget per request (estimated tokens, calibrated never to
    # undercount), and the memories' share. 7,500 is derived from measured needs
    # (docs/DECISIONS.md, D3), not picked.
    context_token_budget: int = Field(default=7500, ge=500)
    memory_token_budget: int = Field(default=600, ge=0)

    # Load the generated demo user (profile, 55 memories, 200 messages) into an empty DB.
    seed_demo_user: bool = True

    # Expose /api/demo/* (trigger failures on the mock world live) and the UI's Demo tab.
    demo_mode: bool = False

    # Background reconciler cadence; 0 disables the loop (the startup pass still runs).
    reconcile_interval_seconds: float = Field(default=30.0, ge=0)

    # How often the provider monitor checks for cancellations (Phase 7, D6); 0 disables.
    # A local list_bookings call per user with a session: cheap, so it can be frequent.
    monitor_interval_seconds: float = Field(default=10.0, ge=0)

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

    @property
    def review_model_spec(self) -> str:
        """The review reader's model: as set; else the explicitly chosen main model;
        else the cheap default."""
        if self.review_model:
            return self.review_model
        return self.model if "model" in self.model_fields_set else DEFAULT_REVIEW_MODEL


def default_model_notice(settings: Settings) -> str | None:
    """A one-line notice naming the models that are defaults, or None if all were chosen.

    "Chosen" means it came from the environment / .env or was passed in (pydantic tracks
    that in `model_fields_set`); a blank `ERRAND_MODEL=` counts as not chosen.
    """
    defaulted: list[str] = []
    if "model" not in settings.model_fields_set:
        defaulted.append(f"orchestrator {settings.model}")
    if not settings.review_model and "model" not in settings.model_fields_set:
        defaulted.append(f"review reader {settings.review_model_spec}")
    if not defaulted:
        return None
    return (
        f"Using default models: {', '.join(defaulted)}"
        " (set ERRAND_MODEL / ERRAND_REVIEW_MODEL in backend/.env to change)"
    )


def load_settings() -> Settings:
    """Settings from the environment / .env (what the running app uses)."""
    # The derived paths are filled by `_derive_paths` before validation, which a
    # static type checker can't see, so the no-argument call looks incomplete to it.
    return Settings()  # pyright: ignore[reportCallIssue]
