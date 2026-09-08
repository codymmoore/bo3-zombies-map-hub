"""Steam Workshop metadata. The backend is the only thing that talks to Steam:
clients send a workshop ID or URL and we fetch everything else here."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from app.errors import SteamNotFound, SteamUnavailable, SteamWrongApp

PUBLISHED_FILE_DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
PLAYER_SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"

_DIGITS = re.compile(r"^\d{1,20}$")


@dataclass(frozen=True)
class WorkshopItem:
    workshop_id: str
    title: str
    description: str | None
    author_steam_id: str | None
    author_name: str | None
    thumbnail_url: str | None
    subscribers: int
    created_at: datetime | None
    updated_at: datetime | None
    consumer_app_id: str | None


def parse_workshop_id(value: str) -> str | None:
    """Accepts a bare numeric ID or any steamcommunity workshop URL with ?id=."""
    value = value.strip()
    if _DIGITS.match(value):
        return value
    try:
        parsed = urlparse(value if "://" in value else f"https://{value}")
    except ValueError:
        return None
    host = parsed.netloc.lower()
    if host != "steamcommunity.com" and not host.endswith(".steamcommunity.com"):
        return None
    ids = parse_qs(parsed.query).get("id")
    if ids and _DIGITS.match(ids[0]):
        return ids[0]
    return None


def _ts(value: int | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


class SteamClient:
    def __init__(self, api_key: str | None = None, app_id: str | None = None, client: httpx.Client | None = None):
        self._api_key = api_key
        self._app_id = app_id or None
        self._client = client or httpx.Client(timeout=10.0)

    def fetch_item(self, workshop_id: str) -> WorkshopItem:
        try:
            resp = self._client.post(
                PUBLISHED_FILE_DETAILS_URL,
                data={"itemcount": "1", "publishedfileids[0]": workshop_id},
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SteamUnavailable(str(exc)) from exc

        try:
            details = body["response"]["publishedfiledetails"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise SteamUnavailable("malformed response") from exc

        # result 1 = OK; 9 = file not found. Hidden/private items also lack a title.
        if details.get("result") != 1 or "title" not in details:
            raise SteamNotFound(workshop_id)

        consumer_app_id = str(details["consumer_app_id"]) if details.get("consumer_app_id") is not None else None
        if self._app_id and consumer_app_id != self._app_id:
            raise SteamWrongApp(consumer_app_id)

        author_steam_id = str(details["creator"]) if details.get("creator") else None
        return WorkshopItem(
            workshop_id=str(details.get("publishedfileid", workshop_id)),
            title=details["title"],
            description=details.get("description") or None,
            author_steam_id=author_steam_id,
            author_name=self._fetch_author_name(author_steam_id),
            thumbnail_url=details.get("preview_url") or None,
            subscribers=int(details.get("subscriptions") or 0),
            created_at=_ts(details.get("time_created")),
            updated_at=_ts(details.get("time_updated")),
            consumer_app_id=consumer_app_id,
        )

    def _fetch_author_name(self, steam_id: str | None) -> str | None:
        """Needs a Steam Web API key; silently skipped without one."""
        if not steam_id or not self._api_key:
            return None
        try:
            resp = self._client.get(PLAYER_SUMMARIES_URL, params={"key": self._api_key, "steamids": steam_id})
            resp.raise_for_status()
            players = resp.json()["response"]["players"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        return players[0].get("personaname") if players else None
