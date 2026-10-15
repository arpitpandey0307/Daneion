"""Stable error codes returned to workers and the console (Scheduler spec error table)."""

from __future__ import annotations


class DaneionError(Exception):
    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str = "", code: str | None = None, status: int | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        if status:
            self.status = status


class NotFound(DaneionError):
    status, code = 404, "NOT_FOUND"


class Unauthorized(DaneionError):
    status, code = 401, "UNAUTHORIZED"


class StaleAttempt(DaneionError):
    """Fencing check failed: the attempt is no longer the task's current lease."""

    status, code = 409, "STALE_ATTEMPT"


class InvalidRequest(DaneionError):
    status, code = 422, "INVALID_REQUEST"


class ClusterFull(DaneionError):
    status, code = 409, "CLUSTER_FULL"


class Conflict(DaneionError):
    status, code = 409, "CONFLICT"


class PairingFailed(DaneionError):
    status, code = 401, "PAIRING_FAILED"


class RateLimited(DaneionError):
    status, code = 429, "RATE_LIMITED"
