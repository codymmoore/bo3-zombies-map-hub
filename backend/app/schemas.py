from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# -- users / auth -------------------------------------------------------------


class UserOut(ORMModel):
    discord_id: str
    username: str
    display_name: str
    avatar_url: str | None


class MeOut(UserOut):
    auth_via: Literal["cookie", "token"]


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class TokenOut(ORMModel):
    id: int
    name: str
    token_prefix: str
    created_at: datetime
    last_used_at: datetime | None


class TokenCreated(TokenOut):
    token: str  # only returned once, at creation


# -- maps ---------------------------------------------------------------------


class MapCreate(BaseModel):
    workshop: str = Field(min_length=1, max_length=500, description="Workshop item ID or steamcommunity URL")


class MapOut(ORMModel):
    id: int
    workshop_id: str
    workshop_url: str
    title: str
    author_steam_id: str | None
    author_name: str | None
    thumbnail_url: str | None
    description: str | None
    subscribers: int
    workshop_created_at: datetime | None
    workshop_updated_at: datetime | None
    added_by_discord_id: str
    added_at: datetime
    times_played: int
    last_played_at: datetime | None
    avg_rating: float | None = None
    rating_count: int = 0
    my_rating: int | None = None


class MapPage(BaseModel):
    items: list[MapOut]
    total: int
    page: int
    page_size: int


class MapLookupOut(BaseModel):
    # workshop_id -> library map id, for IDs that are already in the library
    found: dict[str, int]


class RatingCreate(BaseModel):
    stars: int = Field(ge=1, le=5)
    round_id: int | None = None


class RatingOut(ORMModel):
    id: int
    map_id: int
    discord_user_id: str
    stars: int
    round_id: int | None
    created_at: datetime


# -- guilds -------------------------------------------------------------------


class GuildOut(BaseModel):
    guild_id: str
    name: str
    icon_url: str | None
    notification_channel_id: str
    is_admin: bool


class GuildConfigUpdate(BaseModel):
    notification_channel_id: str = Field(min_length=1, max_length=32)


class GuildConfigOut(ORMModel):
    guild_id: str
    notification_channel_id: str
    created_at: datetime


class ChannelOut(BaseModel):
    channel_id: str
    name: str


# -- sessions / rounds --------------------------------------------------------


class SessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    guild_id: str = Field(min_length=1, max_length=32)


class SessionPlayerOut(BaseModel):
    discord_user_id: str
    user: UserOut | None
    joined_at: datetime
    is_host: bool


class RoundPlayerOut(BaseModel):
    discord_user_id: str
    user: UserOut | None
    ready_at: datetime | None


class RoundOut(BaseModel):
    id: int
    session_id: int
    round_number: int
    status: str
    map: MapOut
    started_at: datetime
    playing_started_at: datetime | None
    ended_at: datetime | None
    message_id: str | None
    players: list[RoundPlayerOut]
    ready_count: int
    player_count: int


class SessionOut(BaseModel):
    id: int
    name: str
    status: str
    host_discord_id: str
    guild_id: str
    thread_id: str | None
    root_message_id: str | None
    started_at: datetime
    ended_at: datetime | None
    player_count: int
    current_round_number: int | None


class SessionDetail(SessionOut):
    players: list[SessionPlayerOut]
    current_round: RoundOut | None
    rounds: list[RoundOut]


class SessionInternalPatch(BaseModel):
    thread_id: str | None = Field(default=None, max_length=32)
    root_message_id: str | None = Field(default=None, max_length=32)


class RoundCreate(BaseModel):
    map_id: int


class RoundPatch(BaseModel):
    status: Literal["playing", "completed", "skipped"] | None = None
    map_id: int | None = None


class RoundInternalPatch(BaseModel):
    message_id: str = Field(min_length=1, max_length=32)


class ReadyBody(BaseModel):
    ready: bool = True


# -- bot events ---------------------------------------------------------------


class EventOut(ORMModel):
    id: int
    type: str
    session_id: int | None
    payload: dict[str, Any]
    created_at: datetime


class EventAck(BaseModel):
    # Explicit IDs, not a high-water mark: only what the bot actually processed.
    ids: list[int] = Field(min_length=1, max_length=200)
