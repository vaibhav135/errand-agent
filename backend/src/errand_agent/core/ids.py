"""ID generation: prefixed, injectable identifiers.

What: An `IdGen` protocol (`new(prefix) -> "prefix_..."`) with `RandomIdGen`
      (uuid4, for real runs) and `SequentialIdGen` (`run_0001`, for tests).

Why:  IDs appear in traces, approvals and idempotency keys. Tests need them
      stable so traces and eval output are identical run to run; the prefix
      tells a reader what kind of thing an ID is (`run_`, `appr_`, `idem_`).

How:  Prefixes are lowercase letters only, so the kind is everything before
      the first underscore. `SequentialIdGen` keeps one counter per prefix.
      Caveat: a fresh `SequentialIdGen` restarts at 0001, so a test that
      simulates a process restart must reuse the same generator instance, or a
      new idempotency key could collide with one persisted before the restart.
"""

import re
import uuid
from collections import defaultdict
from typing import Protocol, runtime_checkable

_PREFIX = re.compile(r"[a-z]+")


def _check_prefix(prefix: str) -> None:
    if not _PREFIX.fullmatch(prefix):
        raise ValueError(f"invalid id prefix {prefix!r}: lowercase letters only")


@runtime_checkable
class IdGen(Protocol):
    def new(self, prefix: str) -> str: ...


class RandomIdGen:
    def new(self, prefix: str) -> str:
        _check_prefix(prefix)
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIdGen:
    def __init__(self) -> None:
        self._counters: defaultdict[str, int] = defaultdict(int)

    def new(self, prefix: str) -> str:
        _check_prefix(prefix)
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:04d}"
