"""Token estimate: a deterministic, deliberately pessimistic count for budgeting.

What: `estimate_tokens(text)` = ceil(len(text) / 3.5).

Why:  The context budget must be enforced offline and reproducibly (evals run without
      network). An exact count needs the provider's tokenizer or API.

How:  English averages roughly 4 characters per token, so dividing by 3.5
      overestimates: packets built to this estimate stay under the real budget. An
      exact counter can replace this behind the same function if ever needed.
"""

import math

CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)
