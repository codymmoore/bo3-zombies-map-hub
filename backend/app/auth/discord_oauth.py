"""Discord OAuth2 with the `identify` scope only. Establishes who the user is and
nothing more; guild membership is checked separately with the bot token."""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.errors import DiscordUnavailable

SCOPE = "identify"


@dataclass(frozen=True)
class DiscordIdentity:
    discord_id: str
    username: str
    global_name: str | None
    avatar: str | None


class DiscordOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, api_base: str, client: httpx.Client | None = None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=10.0)

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "redirect_uri": self._redirect_uri,
                "scope": SCOPE,
                "state": state,
                "prompt": "none",
            }
        )
        return f"https://discord.com/oauth2/authorize?{query}"

    def exchange_code(self, code: str) -> DiscordIdentity:
        try:
            token_resp = self._client.post(
                f"{self._api_base}/oauth2/token",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            access_token = token_resp.json()["access_token"]
            me_resp = self._client.get(
                f"{self._api_base}/users/@me", headers={"Authorization": f"Bearer {access_token}"}
            )
            me_resp.raise_for_status()
            me = me_resp.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise DiscordUnavailable(str(exc)) from exc

        return DiscordIdentity(
            discord_id=str(me["id"]),
            username=me.get("username", ""),
            global_name=me.get("global_name"),
            avatar=me.get("avatar"),
        )
