import re

import pytest

from errand_agent.core.ids import IdGen, RandomIdGen, SequentialIdGen


def test_both_generators_satisfy_the_protocol() -> None:
    assert isinstance(RandomIdGen(), IdGen)
    assert isinstance(SequentialIdGen(), IdGen)


def test_random_ids_are_prefixed_and_unique() -> None:
    gen = RandomIdGen()

    ids = {gen.new("run") for _ in range(1000)}

    assert len(ids) == 1000
    assert all(re.fullmatch(r"run_[0-9a-f]{32}", i) for i in ids)


def test_sequential_ids_count_per_prefix() -> None:
    gen = SequentialIdGen()

    assert gen.new("run") == "run_0001"
    assert gen.new("run") == "run_0002"
    assert gen.new("appr") == "appr_0001"  # independent counter per prefix
    assert gen.new("run") == "run_0003"


@pytest.mark.parametrize("bad", ["", "Run", "bk-1", "run_", "has space"])
def test_invalid_prefix_rejected(bad: str) -> None:
    # Prefixes are lowercase letters only, so an ID's kind is always parseable
    # as everything before the first underscore.
    with pytest.raises(ValueError):
        RandomIdGen().new(bad)
    with pytest.raises(ValueError):
        SequentialIdGen().new(bad)
