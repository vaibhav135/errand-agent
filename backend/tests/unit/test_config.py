from pathlib import Path

import pytest
from pydantic import ValidationError

from errand_agent.core.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from the developer's shell: drop any ERRAND_* already exported.
    import os

    for key in list(os.environ):
        if key.startswith("ERRAND_"):
            monkeypatch.delenv(key)


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
