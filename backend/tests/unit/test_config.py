from pathlib import Path

import pytest
from pydantic import ValidationError

from errand_agent.core.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from the developer's shell: drop any ERRAND_* or provider keys exported.
    import os

    for key in list(os.environ):
        if key.startswith("ERRAND_") or key in PROVIDER_KEY_VARS:
            monkeypatch.delenv(key)


PROVIDER_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")


def make_settings(**overrides: object) -> Settings:
    # _env_file=None: never read the developer's .env in tests.
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_defaults() -> None:
    s = make_settings()

    assert s.data_dir == Path("var")
    assert s.model is None
    assert s.mock_seed == 42


def test_paths_derive_from_data_dir(tmp_path: Path) -> None:
    s = make_settings(data_dir=tmp_path)

    assert s.agent_db_path == tmp_path / "agent.db"
    assert s.mock_db_path == tmp_path / "mock_world.db"
    assert s.trace_dir == tmp_path / "traces"


def test_individual_path_override_wins(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere" / "a.db"

    s = make_settings(data_dir=tmp_path, agent_db_path=custom)

    assert s.agent_db_path == custom
    assert s.mock_db_path == tmp_path / "mock_world.db"  # others still derived


def test_reads_prefixed_env_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ERRAND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ERRAND_MODEL", "anthropic:claude-sonnet-5-5")
    monkeypatch.setenv("ERRAND_MOCK_SEED", "7")

    s = make_settings()

    assert s.data_dir == tmp_path
    assert s.model == "anthropic:claude-sonnet-5-5"
    assert s.mock_seed == 7
    assert s.trace_dir == tmp_path / "traces"


def test_invalid_value_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERRAND_MOCK_SEED", "not-a-number")

    with pytest.raises(ValidationError):
        make_settings()


# --- provider API keys ----------------------------------------------------------------


def test_provider_keys_default_to_none() -> None:
    s = make_settings()

    assert s.anthropic_api_key is None
    assert s.openai_api_key is None
    assert s.google_api_key is None


@pytest.mark.parametrize(
    ("env_var", "field"),
    [
        ("ANTHROPIC_API_KEY", "anthropic_api_key"),
        ("OPENAI_API_KEY", "openai_api_key"),
        ("GOOGLE_API_KEY", "google_api_key"),
    ],
)
def test_provider_keys_use_standard_unprefixed_names(
    monkeypatch: pytest.MonkeyPatch, env_var: str, field: str
) -> None:
    # Standard names (not ERRAND_*), so a key already exported in the shell works.
    monkeypatch.setenv(env_var, "sk-test-123")

    secret = getattr(make_settings(), field)

    assert secret.get_secret_value() == "sk-test-123"


def test_provider_keys_never_appear_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")

    s = make_settings()

    assert "sk-very-secret" not in repr(s)
    assert "sk-very-secret" not in str(s.anthropic_api_key)


def test_keys_and_settings_are_read_from_an_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-from-file\nERRAND_MOCK_SEED=9\n")

    s = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-from-file"
    assert s.mock_seed == 9


def test_blank_values_in_env_file_mean_unset(tmp_path: Path) -> None:
    # .env.example ships with `ANTHROPIC_API_KEY=` and `ERRAND_MODEL=`; copied as-is,
    # those must read as "not configured", not as empty strings.
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=\nERRAND_MODEL=\n")

    s = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert s.anthropic_api_key is None
    assert s.model is None


def test_env_example_lists_every_provider_key() -> None:
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()

    for var in (*PROVIDER_KEY_VARS, "ERRAND_MODEL"):
        assert f"{var}=" in example
