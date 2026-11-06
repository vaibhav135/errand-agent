"""History scrubbing: forgotten facts and saved PII leave the stored conversation.

What: `scrub_history(conn, user_id, replacements)` rewrites the user's stored messages;
      `phrase_pattern()` and `phone_pattern()` build the matchers.

Why:  Selecting memories carefully isn't enough if the raw transcript repeats them: a
      forgotten "I'm allergic to cats" or a typed phone number would otherwise come
      back to the model through the recent-conversation window.

How:  - Forgetting an explicit memory replaces its quoted evidence with "[forgotten]".
      - Saving profile details replaces the name, phone (any spacing / dashes, with or
        without +91), email and street line with "[name saved]" etc.; the area stays.
      - Matching ignores case and spacing; the rest of each message is untouched; only
        that user's messages (all sessions, both roles) are rewritten.
      - Scope, stated honestly: the model sees a typed value once, in the turn that
        extracts and saves it. From then on it is gone from the stored history.
"""

import re
import sqlite3

Replacement = tuple[re.Pattern[str], str]


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = phrase.split()
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)


def phone_pattern(normalised: str) -> re.Pattern[str]:
    """Match a +91XXXXXXXXXX number however it was typed."""
    digits = re.sub(r"\D", "", normalised)[-10:]
    return re.compile(r"(?:\+?91[\s-]*)?" + r"[\s-]*".join(digits))


def scrub_history(conn: sqlite3.Connection, user_id: str, replacements: list[Replacement]) -> int:
    """Apply replacements to the user's stored messages; returns how many changed."""
    rows = conn.execute(
        "SELECT m.message_id, m.content FROM messages m JOIN sessions s USING (session_id)"
        " WHERE s.user_id = ?",
        (user_id,),
    ).fetchall()
    changed = 0
    for row in rows:
        content = row["content"]
        for pattern, replacement in replacements:
            content = pattern.sub(replacement, content)
        if content != row["content"]:
            conn.execute(
                "UPDATE messages SET content = ? WHERE message_id = ?", (content, row["message_id"])
            )
            changed += 1
    return changed
