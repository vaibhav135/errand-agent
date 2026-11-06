"""Token estimate: a deterministic count for budgeting, calibrated to never undercount.

What: `estimate_tokens(text)` for text (prose, instructions, tool schemas),
      `estimate_json_tokens(text)` for JSON data (tool calls and results), and
      `TOOL_USE_OVERHEAD_TOKENS`, the hidden prompt a provider adds whenever tools
      are offered.

Why:  The context budget must be enforced offline and reproducibly (evals run without
      a network), and Claude's tokenizer isn't published. The estimate must never be
      *below* the real count, or the cap doesn't hold (BUG-7: the old chars/3.5 rule
      undercounted by ~45%; newer Claude tokenizers produce more tokens per character).

How:  Ratios measured with Anthropic's count_tokens on our own texts
      (`make calibrate` -> tests/data/token_calibration.json; claude-sonnet-5-5,
      2026-11-06), then set ~7% below the lowest measured value:
      - text: 2.58-3.00 chars/token measured (schemas, system prompt, chat) -> 2.4
      - JSON data: 2.04 measured (a booking list) -> 1.9 (keys, quotes and ids
        tokenize densely)
      - tool-use overhead: 321 measured -> 350
      A test fails if any recorded sample is estimated below its real count. Each
      `model_call` trace event also shows the estimate next to the real count, so drift
      after a model change is visible; re-run `make calibrate` then.
"""

import math

TEXT_CHARS_PER_TOKEN = 2.4
JSON_CHARS_PER_TOKEN = 1.9
TOOL_USE_OVERHEAD_TOKENS = 350


def estimate_tokens(text: str) -> int:
    """Prose, instructions and tool schemas."""
    return math.ceil(len(text) / TEXT_CHARS_PER_TOKEN)


def estimate_json_tokens(text: str) -> int:
    """JSON data: tool call arguments and tool results."""
    return math.ceil(len(text) / JSON_CHARS_PER_TOKEN)
