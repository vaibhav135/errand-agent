"""Keyed randomness: reproducible "random" decisions for the mock world.

What: `keyed_unit(seed, *parts)` returns a float in [0, 1) that depends only on
      its arguments, e.g. `keyed_unit(42, "book", hold_id, attempt)`.

Why:  A single seeded RNG stream makes every outcome depend on *call order*: one
      extra search shifts every later booking result and breaks unrelated
      tests. Keying each decision by what it is about (operation + reference)
      keeps outcomes stable however the agent sequences its calls, while still
      giving the contract's distributions (70/15/15) across many keys.

How:  sha256 over the joined parts, first 8 bytes as an integer, scaled to
      [0, 1). Python's built-in `hash()` is NOT used: for strings it is salted
      per process (PYTHONHASHSEED), so results would change on every restart.
"""

import hashlib

_SCALE = float(1 << 64)


def keyed_unit(seed: int, *parts: object) -> float:
    key = "\x1f".join(str(p) for p in (seed, *parts))  # unit separator: no ambiguity
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") / _SCALE
