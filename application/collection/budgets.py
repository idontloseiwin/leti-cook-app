"""Pure request-attempt budget for one sequential collection pass or chunk.

Not wired into inventory execution yet. The future caller must call try_debit
immediately before requesting a provider page; failed requests are not refunded.
This counter owns no provider, database, clock or cross-worker coordination.
"""


class PageBudget:
    """A nonnegative limit with read-only attempt and remaining counters."""

    __slots__ = ("_limit", "_attempts")

    def __init__(self, limit: int):
        if type(limit) is not int:
            raise TypeError("page budget limit must be an integer")
        if limit < 0:
            raise ValueError("page budget limit must be nonnegative")
        self._limit = limit
        self._attempts = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def remaining(self) -> int:
        return self._limit - self._attempts

    def try_debit(self) -> bool:
        """Reserve one request attempt; False means the caller must not send it."""
        if self._attempts >= self._limit:
            return False
        self._attempts += 1
        return True
