"""Test harness: in-memory SQLite, fake Discord + Steam, cookie login helper."""

import os

# Settings are read once (lru_cache), so pin test values before importing the app.
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-hs256")
os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import deps
from app.auth.cookies import issue_session_token
from app.config import get_settings
from app.db import get_db
from app.errors import DiscordUnavailable, SteamNotFound, SteamWrongApp
from app.main import create_app
from app.models import Base, DiscordUser, GuildConfig
from app.services.membership import ChannelInfo, GuildInfo, MemberInfo
from app.services.steam import WorkshopItem

GUILD = "100000000000000001"
CHANNEL = "200000000000000001"
HOST = "300000000000000001"
ALICE = "300000000000000002"
BOB = "300000000000000003"
OUTSIDER = "300000000000000009"
ADMIN_ROLE = "400000000000000001"


class FakeMembership:
    def __init__(self):
        self.members: dict[str, dict[str, MemberInfo]] = {}
        self.owners: dict[str, str] = {}
        self.available = True
        self.calls = 0

    def add(self, guild_id: str, user_id: str, *, admin: bool = False) -> None:
        roles = (ADMIN_ROLE,) if admin else ()
        self.members.setdefault(guild_id, {})[user_id] = MemberInfo(user_id=user_id, roles=roles)

    def remove(self, guild_id: str, user_id: str) -> None:
        self.members.get(guild_id, {}).pop(user_id, None)

    def _check(self):
        self.calls += 1
        if not self.available:
            raise DiscordUnavailable("fake outage")

    def get_member(self, guild_id, user_id):
        self._check()
        return self.members.get(guild_id, {}).get(user_id)

    def is_member(self, guild_id, user_id):
        return self.get_member(guild_id, user_id) is not None

    def is_admin(self, guild_id, user_id):
        member = self.get_member(guild_id, user_id)
        if member is None:
            return False
        return self.owners.get(guild_id) == user_id or ADMIN_ROLE in member.roles

    def get_guild(self, guild_id):
        self._check()
        if guild_id not in self.members:
            return None
        return GuildInfo(guild_id=guild_id, name=f"Guild {guild_id}", icon=None, owner_id=self.owners.get(guild_id, ""))

    def get_text_channels(self, guild_id):
        self._check()
        return [ChannelInfo(channel_id=CHANNEL, name="zombies", position=0)]

    def clear_cache(self):
        pass


class FakeSteam:
    def __init__(self):
        self.items: dict[str, WorkshopItem] = {}
        self.calls: list[str] = []

    def add(self, workshop_id: str, title: str = "Test Map", app_id: str = "311210", **kw) -> WorkshopItem:
        item = WorkshopItem(
            workshop_id=workshop_id,
            title=title,
            description=kw.get("description", "desc"),
            author_steam_id=kw.get("author_steam_id", "76561198000000001"),
            author_name=kw.get("author_name", "Mapper"),
            thumbnail_url=kw.get("thumbnail_url", "https://img/preview.jpg"),
            subscribers=kw.get("subscribers", 100),
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
            consumer_app_id=app_id,
        )
        self.items[workshop_id] = item
        return item

    def fetch_item(self, workshop_id: str) -> WorkshopItem:
        self.calls.append(workshop_id)
        item = self.items.get(workshop_id)
        if item is None:
            raise SteamNotFound(workshop_id)
        if item.consumer_app_id != "311210":
            raise SteamWrongApp(item.consumer_app_id)
        return item


@pytest.fixture
def engine():
    """In-memory SQLite by default. Set TEST_DATABASE_URL to a Postgres URL to run
    the same suite against the real thing (e.g. the docker-compose db)."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        eng = create_engine(url)
        Base.metadata.drop_all(eng)
    else:
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def db(engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.close()


@pytest.fixture
def membership() -> FakeMembership:
    fm = FakeMembership()
    fm.add(GUILD, HOST)
    fm.add(GUILD, ALICE)
    fm.add(GUILD, BOB)
    return fm


@pytest.fixture
def steam() -> FakeSteam:
    fs = FakeSteam()
    fs.add("111", title="Alpha Map", subscribers=500, author_name="Ann")
    fs.add("222", title="Beta Map", subscribers=50, author_name="Ben")
    fs.add("333", title="Gamma Map", subscribers=5000, author_name="Ann")
    fs.add("999", title="Not BO3", app_id="440")
    return fs


@pytest.fixture
def client(engine, membership, steam):
    app = create_app()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[deps.get_membership] = lambda: membership
    app.dependency_overrides[deps.get_steam] = lambda: steam
    with TestClient(app) as c:
        yield c


class Actor:
    """A logged-in user: a TestClient wrapper that sends this user's cookie."""

    def __init__(self, client: TestClient, discord_id: str):
        self.client = client
        self.discord_id = discord_id
        token = issue_session_token(get_settings().secret_key, discord_id, timedelta(days=1))
        self.cookies = {get_settings().session_cookie_name: token}

    def _call(self, method, url, **kw):
        return self.client.request(method, url, cookies=self.cookies, **kw)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def patch(self, url, **kw):
        return self._call("PATCH", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


@pytest.fixture
def users(db, client):
    """Creates DiscordUser rows and returns logged-in actors keyed by name."""
    ids = {"host": HOST, "alice": ALICE, "bob": BOB, "outsider": OUTSIDER}
    for name, discord_id in ids.items():
        db.add(DiscordUser(discord_id=discord_id, username=name, global_name=name.title()))
    db.commit()
    return {name: Actor(client, discord_id) for name, discord_id in ids.items()}


@pytest.fixture
def guild(db):
    db.add(GuildConfig(guild_id=GUILD, notification_channel_id=CHANNEL))
    db.commit()
    return GUILD


@pytest.fixture
def bot(client):
    class Bot:
        headers = {"X-Internal-Token": get_settings().internal_api_token}

        def get(self, url, **kw):
            return client.get(url, headers=self.headers, **kw)

        def post(self, url, **kw):
            return client.post(url, headers=self.headers, **kw)

        def patch(self, url, **kw):
            return client.patch(url, headers=self.headers, **kw)

        def events(self, after: int = 0):
            rows = self.get("/internal/events?limit=200").json()
            return [e for e in rows if e["id"] > after]

    return Bot()


def add_map(actor: Actor, workshop: str) -> dict:
    resp = actor.post("/maps", json={"workshop": workshop})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()
