"""Transport failures: what goes wrong *between* us and a provider.

What: `ProviderError` and its subclasses `ProviderUnavailable` (503) and
      `ProviderTimeout`.

Why:  Business outcomes (slot taken, hold expired, slot conflict) are normal
      results the workflow branches on, so they are typed result values. Transport
      failures are different: the request may not have been processed at all
      (503) or its outcome is unknown (timeout), so they are exceptions that
      force the caller to retry or consult the source of truth.

How:  `ProviderUnavailable` is safe to retry with the same idempotency key.
      `ProviderTimeout` means "it may or may not have happened": after a `book`
      timeout, call `get_booking_status` before deciding anything.
"""


class ProviderError(Exception):
    """Base class for transport-level provider failures."""


class ProviderUnavailable(ProviderError):
    """The provider answered 503: nothing was done; retry with the same key."""

    status_code = 503


class ProviderTimeout(ProviderError):
    """No answer in time: the operation may or may not have taken effect."""
