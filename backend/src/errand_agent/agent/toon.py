"""TOON: a compact table encoding for list-shaped tool results.

What: `to_toon(name, rows)` -> e.g.
          bookings[2]{booking_id,status}:
            bk_1,confirmed
            bk_2,completed
      Field names once, then one line per row, instead of JSON repeating every key.

Why:  On our data TOON is ~36% fewer real tokens than compact JSON for the bookings
      list, and a live eval found the model reads ids from it at least as reliably
      (10/10 vs JSON's 9/10; docs/DECISIONS.md, D3). Used for find_my_bookings and
      list_memories; search options stay JSON (their nested slot lists are TOON's
      weak spot). TOON is denser than JSON, so it has its own token ratio (1.6).

How:  Only for a non-empty list of dicts sharing the same keys; anything else falls
      back to compact JSON (nested data is TOON's weak spot). A cell that is a dict or
      list is written as compact JSON. A cell containing a comma, quote, newline or
      edge whitespace is quoted JSON-style, so ids and names survive exactly.
"""

import json
from collections.abc import Sequence
from typing import Any


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, separators=(",", ":"))
    text = str(value)
    if not text or any(c in text for c in ',"\n') or text != text.strip():
        return json.dumps(text)
    return text


def to_toon(name: str, rows: Sequence[dict[str, Any]]) -> str:
    """TOON for a uniform list of flat dicts; compact JSON otherwise."""
    if not rows or any(row.keys() != rows[0].keys() for row in rows):
        return json.dumps(list(rows), separators=(",", ":"), default=str)
    fields = list(rows[0])
    lines = [f"{name}[{len(rows)}]{{{','.join(fields)}}}:"]
    lines += ["  " + ",".join(_cell(row[f]) for f in fields) for row in rows]
    return "\n".join(lines)
