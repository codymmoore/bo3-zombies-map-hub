"""FastAPI dependencies: settings, DB session, authentication, service singletons."""

import secrets
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.cookies import read_session_token
from app.auth.discord_oauth import DiscordOAuth
from app.auth.tokens import hash_token
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import ApiToken, DiscordUser, as_utc, utcnow
from app.services.membership import MembershipService
from app.services.steam import SteamClient

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]


@dataclass
class CurrentUser:
    user: DiscordUser
    via: Literal["cookie", "token"]

    @property
    def discord_id(self) -> str:
        return self.user.discord_id


def _user_from_bearer(db: Session, token: str) -> CurrentUser | None:
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token), ApiToken.revoked_at.is_(None)))
    if row is None:
        return None
    user = db.get(DiscordUser, row.discord_user_id)
    if user is None:
        return None
    now = utcnow()
    # Throttle the write: one update per 5 minutes per token.
    if row.last_used_at is None or (now - as_utc(row.last_used_at)) > timedelta(minutes=5):
        row.last_used_at = now
        db.commit()
    return CurrentUser(user=user, via="token")


def _user_from_cookie(db: Session, settings: Settings, request: Request) -> CurrentUser | None:
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    discord_id = read_session_token(settings.secret_key, raw)
    if not discord_id:
        return None
    user = db.get(DiscordUser, discord_id)
    if user is None:
        return None
    return CurrentUser(user=user, via="cookie")


def get_optional_user(request: Request, db: DbDep, settings: SettingsDep) -> CurrentUser | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        # A bearer header is an explicit auth attempt: a dead token must surface as
        # 401 (so the extension can tell the user) rather than degrade to anonymous.
        user = _user_from_bearer(db, auth[7:].strip())
        if user is None:
            raise ApiError(401, "invalid_token", "API token is invalid or revoked")
        return user
    return _user_from_cookie(db, settings, request)


def get_current_user(user: Annotated[CurrentUser | None, Depends(get_optional_user)]) -> CurrentUser:
    if user is None:
        raise ApiError(401, "unauthenticated", "Login required")
    return user


def get_cookie_user(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    """For token management: an API token must not be able to mint more tokens."""
    if user.via != "cookie":
        raise ApiError(403, "cookie_auth_required", "This action requires a browser login")
    return user


def require_bot(settings: SettingsDep, x_internal_token: Annotated[str | None, Header()] = None) -> None:
    if not x_internal_token or not secrets.compare_digest(x_internal_token, settings.internal_api_token):
        raise ApiError(401, "unauthenticated", "Invalid internal token")


UserDep = Annotated[CurrentUser, Depends(get_current_user)]
OptionalUserDep = Annotated[CurrentUser | None, Depends(get_optional_user)]
CookieUserDep = Annotated[CurrentUser, Depends(get_cookie_user)]
BotDep = Depends(require_bot)


@lru_cache
def get_membership() -> MembershipService:
    s = get_settings()
    return MembershipService(s.discord_bot_token, s.discord_api_base, s.membership_cache_ttl_seconds)


@lru_cache
def get_steam() -> SteamClient:
    s = get_settings()
    return SteamClient(api_key=s.steam_api_key, app_id=s.steam_app_id)


@lru_cache
def get_oauth() -> DiscordOAuth:
    s = get_settings()
    return DiscordOAuth(s.discord_client_id, s.discord_client_secret, s.discord_redirect_uri, s.discord_api_base)


MembershipDep = Annotated[MembershipService, Depends(get_membership)]
SteamDep = Annotated[SteamClient, Depends(get_steam)]
OAuthDep = Annotated[DiscordOAuth, Depends(get_oauth)]
