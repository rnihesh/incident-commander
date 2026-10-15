"""Shared error type for outbound actions."""

from __future__ import annotations


class ActionError(Exception):
    """An outbound action (GitHub, Discord) failed.

    ``retryable`` tells the incident machinery whether to stay in ACTING and retry later
    (network failures, 5xx, 429 and GitHub secondary rate limits) or give up (4xx).
    ``retry_after`` is the server's requested wait in seconds, when it gave one.
    Messages never contain tokens or webhook URLs.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after
