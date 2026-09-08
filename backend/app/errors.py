"""API error helpers. All errors carry a machine-readable `code` plus a `message`
so the frontend can branch on them (e.g. "already_in_session")."""

from typing import Any

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        super().__init__(status_code=status_code, detail={"code": code, "message": message, **extra})
        self.code = code


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def forbidden(message: str = "Forbidden", code: str = "forbidden") -> ApiError:
    return ApiError(403, code, message)


def conflict(code: str, message: str, **extra: Any) -> ApiError:
    return ApiError(409, code, message, **extra)


def bad_request(code: str, message: str, **extra: Any) -> ApiError:
    return ApiError(400, code, message, **extra)


class DiscordUnavailable(Exception):
    """Discord's REST API could not be reached and no cached answer exists."""


class SteamUnavailable(Exception):
    """Steam's Web API could not be reached or returned garbage."""


class SteamNotFound(Exception):
    """The workshop item does not exist (or is private/hidden)."""


class SteamWrongApp(Exception):
    def __init__(self, app_id: str | None):
        super().__init__(app_id)
        self.app_id = app_id
