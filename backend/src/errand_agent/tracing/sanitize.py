"""Sanitizer: make tool/model payloads safe and readable before they are traced.

What: `sanitize(value, max_str=500)` returns a JSON-friendly copy of a payload
      with secrets redacted by key name and long strings truncated.

Why:  Traces record tool arguments and results verbatim ("sanitized results" in
      the assignment). They must never contain credentials, and free text such
      as provider reviews can be long enough to drown the useful events.

How:  - Applied automatically by the recorder to every payload, so no call site
        can forget it.
      - Redaction matches whole key names or known suffixes after normalising
        (lowercase, '-' -> '_'): `api_key`, `password`, `authorization`,
        `*_secret`, `access_token`, ... A plain "contains 'token'" rule is
        deliberately avoided: it would also wipe `input_tokens`/`output_tokens`.
      - Strings longer than `max_str` keep a prefix plus "…[+N chars]".
      - Dicts and lists are copied recursively (tuples become lists); the input
        is never mutated. Datetimes are left for the JSONL writer to format.
      - Marking free text as *untrusted* is not done here: that matters where a
        model reads it, i.e. the quarantined review reader (Phase 5).
"""

from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "password", "passwd", "secret", "authorization", "token", "cookie"}
)
_SECRET_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_password",
    "_secret",
    "access_token",
    "refresh_token",
    "auth_token",
    "id_token",
    "session_token",
)


def _is_secret_key(key: object) -> bool:
    norm = str(key).lower().replace("-", "_")
    return norm in _SECRET_KEYS or norm.endswith(_SECRET_SUFFIXES)


def sanitize(value: Any, *, max_str: int = 500) -> Any:
    if isinstance(value, Mapping):
        return {
            k: REDACTED if _is_secret_key(k) else sanitize(v, max_str=max_str)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize(v, max_str=max_str) for v in value]
    if isinstance(value, str) and len(value) > max_str:
        return f"{value[:max_str]}…[+{len(value) - max_str} chars]"
    return value
