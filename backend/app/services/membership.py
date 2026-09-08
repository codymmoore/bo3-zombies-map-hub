"""Guild membership and admin checks, done server-side with the bot token.

This is the ONLY place that talks to Discord's guild/member REST endpoints. The
OAuth login uses the `identify` scope only, so this lookup is what tells us which
servers a user is in (and their roles, which the `guilds` scope couldn't give us).

Caching is a short TTL for rate-limit protection, not correctness. When Discord
is unreachable we serve a stale cached answer if we have one; otherwise we raise
DiscordUnavailable and the caller decides (joins fail closed, reads 503 too).
"""

import threading
import time
from dataclasses import dataclass, field

import httpx

from app.errors import DiscordUnavailable

PERM_ADMINISTRATOR = 1 << 3
PERM_MANAGE_GUILD = 1 << 5
TEXT_CHANNEL_TYPES = {0, 5}  # GUILD_TEXT, GUILD_ANNOUNCEMENT


@dataclass(frozen=True)
class MemberInfo:
    user_id: str
    roles: tuple[str, ...]
    nick: str | None = None


@dataclass(frozen=True)
class GuildInfo:
    guild_id: str
    name: str
    icon: str | None
    owner_id: str
    role_permissions: dict[str, int] = field(default_factory=dict)

    @property
    def icon_url(self) -> str | None:
        if not self.icon:
            return None
        ext = "gif" if self.icon.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/icons/{self.guild_id}/{self.icon}.{ext}"


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    name: str
    position: int


class _TtlCache:
    """Expired entries are kept on purpose (stale answers during a Discord outage),
    so growth is bounded by `max_size` instead: when full, expired entries go
    first, then the oldest."""

    def __init__(self, ttl: float, max_size: int = 5000):
        self._ttl = ttl
        self._max_size = max_size
        self._data: dict[object, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: object) -> tuple[bool, object]:
        """Returns (is_fresh, value). Missing keys raise KeyError."""
        with self._lock:
            expires, value = self._data[key]
        return time.monotonic() < expires, value

    def set(self, key: object, value: object) -> None:
        now = time.monotonic()
        with self._lock:
            self._data[key] = (now + self._ttl, value)
            if len(self._data) > self._max_size:
                self._evict(now)

    def _evict(self, now: float) -> None:
        for k in [k for k, (expires, _) in self._data.items() if expires <= now]:
            del self._data[k]
        overflow = len(self._data) - self._max_size
        if overflow > 0:
            for k in sorted(self._data, key=lambda k: self._data[k][0])[:overflow]:
                del self._data[k]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class MembershipService:
    def __init__(self, bot_token: str, api_base: str, ttl_seconds: float = 90, client: httpx.Client | None = None):
        self._api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(
            timeout=8.0,
            headers={"Authorization": f"Bot {bot_token}", "User-Agent": "bo3-map-hub (backend)"},
        )
        self._members = _TtlCache(ttl_seconds)
        self._guilds = _TtlCache(ttl_seconds * 4)

    # -- public API ---------------------------------------------------------

    def is_member(self, guild_id: str, user_id: str) -> bool:
        return self.get_member(guild_id, user_id) is not None

    def get_member(self, guild_id: str, user_id: str) -> MemberInfo | None:
        """None means "not a member". Raises DiscordUnavailable on outage without cache."""
        key = (guild_id, user_id)
        return self._cached(self._members, key, lambda: self._fetch_member(guild_id, user_id))

    def is_admin(self, guild_id: str, user_id: str) -> bool:
        """Guild owner, or holds a role with Administrator / Manage Server."""
        member = self.get_member(guild_id, user_id)
        if member is None:
            return False
        guild = self.get_guild(guild_id)
        if guild is None:
            return False
        if guild.owner_id == user_id:
            return True
        for role_id in member.roles:
            perms = guild.role_permissions.get(role_id, 0)
            if perms & (PERM_ADMINISTRATOR | PERM_MANAGE_GUILD):
                return True
        return False

    def get_guild(self, guild_id: str) -> GuildInfo | None:
        """None if the bot is not in the guild (or it doesn't exist)."""
        return self._cached(self._guilds, guild_id, lambda: self._fetch_guild(guild_id))

    def get_text_channels(self, guild_id: str) -> list[ChannelInfo]:
        data = self._request("GET", f"/guilds/{guild_id}/channels")
        if data is None:
            return []
        channels = [
            ChannelInfo(channel_id=str(c["id"]), name=c.get("name", ""), position=int(c.get("position", 0)))
            for c in data
            if c.get("type") in TEXT_CHANNEL_TYPES
        ]
        return sorted(channels, key=lambda c: c.position)

    def clear_cache(self) -> None:
        self._members.clear()
        self._guilds.clear()

    # -- internals ----------------------------------------------------------

    def _cached(self, cache: _TtlCache, key, fetch):
        try:
            fresh, value = cache.get(key)
        except KeyError:
            fresh, value, have = False, None, False
        else:
            have = True
        if fresh:
            return value
        try:
            value = fetch()
        except DiscordUnavailable:
            if have:
                return value  # stale but better than nothing for reads
            raise
        cache.set(key, value)
        return value

    def _fetch_member(self, guild_id: str, user_id: str) -> MemberInfo | None:
        data = self._request("GET", f"/guilds/{guild_id}/members/{user_id}")
        if data is None:
            return None
        return MemberInfo(
            user_id=str(data["user"]["id"]),
            roles=tuple(str(r) for r in data.get("roles", [])),
            nick=data.get("nick"),
        )

    def _fetch_guild(self, guild_id: str) -> GuildInfo | None:
        data = self._request("GET", f"/guilds/{guild_id}")
        if data is None:
            return None
        roles = {str(r["id"]): int(r.get("permissions", 0)) for r in data.get("roles", [])}
        return GuildInfo(
            guild_id=str(data["id"]),
            name=data.get("name", ""),
            icon=data.get("icon"),
            owner_id=str(data.get("owner_id", "")),
            role_permissions=roles,
        )

    def _request(self, method: str, path: str):
        """Returns parsed JSON, or None for 404 (and 403: bot not in that guild)."""
        try:
            resp = self._client.request(method, f"{self._api_base}{path}")
        except httpx.HTTPError as exc:
            raise DiscordUnavailable(str(exc)) from exc
        if resp.status_code in (403, 404):
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise DiscordUnavailable(f"discord returned {resp.status_code}")
        if resp.status_code != 200:
            raise DiscordUnavailable(f"unexpected status {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise DiscordUnavailable("malformed response") from exc
