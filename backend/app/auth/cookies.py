"""Signed login cookie for the web app (JWT, HS256)."""

from datetime import datetime, timedelta, timezone

import jwt

ALGORITHM = "HS256"


def issue_session_token(secret: str, discord_id: str, max_age: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": discord_id, "iat": now, "exp": now + max_age, "kind": "session"},
        secret,
        algorithm=ALGORITHM,
    )


def read_session_token(secret: str, token: str) -> str | None:
    """Returns the discord_id, or None if the token is invalid/expired."""
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    if claims.get("kind") != "session":
        return None
    sub = claims.get("sub")
    return str(sub) if sub else None


def issue_state_token(secret: str, next_url: str | None, nonce: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"kind": "oauth_state", "next": next_url, "nonce": nonce, "iat": now, "exp": now + timedelta(minutes=10)},
        secret,
        algorithm=ALGORITHM,
    )


def read_state_token(secret: str, token: str) -> dict | None:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    if claims.get("kind") != "oauth_state":
        return None
    return claims
