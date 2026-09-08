"""Per-user API tokens for the Chrome extension. Plaintext is shown once; only
a SHA-256 hash is persisted."""

import hashlib
import secrets

TOKEN_PREFIX = "hub_"


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def display_prefix(token: str) -> str:
    return token[:12]
