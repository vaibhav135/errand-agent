"""TOON: a compact table encoding for list-shaped tool results (experiment).

What: `to_toon(name, rows)` -> e.g.
          bookings[2]{booking_id,status}:
            bk_1,confirmed
            bk_2,completed
      Field names once, then one line per row, instead of JSON repeating every key.

Why:  Measured on our data, TOON was 27-40% fewer real tokens than compact JSON for
      flat lists (docs/DECISIONS.md, D3). Being evaluated on the `experiment/toon`
      branch: kept only if a live eval shows the model still picks the right ids.

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
