"""SQLAlchemy models. See CLAUDE.md "Data model" for the design rationale."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Normalize DB datetimes: Postgres returns aware values, SQLite naive (UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SessionStatus:
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RoundStatus:
    DOWNLOADING = "downloading"
    PLAYING = "playing"
    COMPLETED = "completed"
    SKIPPED = "skipped"

    OPEN = (DOWNLOADING, PLAYING)
    CLOSED = (COMPLETED, SKIPPED)


class EventType:
    SESSION_CREATED = "session.created"
    SESSION_ENDED = "session.ended"
    ROUND_MAP_SELECTED = "round.map_selected"
    ROUND_READY_CHANGED = "round.ready_changed"
    ROUND_STARTED = "round.started"
    ROUND_ENDED = "round.ended"


class Base(DeclarativeBase):
    pass


# Discord snowflakes and Steam IDs are stored as strings: they exceed JS's safe
# integer range, so keeping them as strings end-to-end avoids precision bugs.
ID = String(32)


class DiscordUser(Base):
    """Users who have logged in at least once. Needed to render names on rosters."""

    __tablename__ = "discord_users"

    discord_id: Mapped[str] = mapped_column(ID, primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    global_name: Mapped[str | None] = mapped_column(String(64))
    avatar: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def display_name(self) -> str:
        return self.global_name or self.username

    @property
    def avatar_url(self) -> str | None:
        if not self.avatar:
            return None
        ext = "gif" if self.avatar.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{self.discord_id}/{self.avatar}.{ext}"


class ApiToken(Base):
    """Long-lived per-user tokens for the Chrome extension. Only the hash is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(ID, ForeignKey("discord_users.discord_id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_prefix: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GuildConfig(Base):
    __tablename__ = "guild_configs"

    guild_id: Mapped[str] = mapped_column(ID, primary_key=True)
    notification_channel_id: Mapped[str] = mapped_column(ID)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Map(Base):
    __tablename__ = "maps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workshop_id: Mapped[str] = mapped_column(ID, unique=True)
    title: Mapped[str] = mapped_column(String(255))
    author_steam_id: Mapped[str | None] = mapped_column(ID)
    author_name: Mapped[str | None] = mapped_column(String(255))
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    subscribers: Mapped[int] = mapped_column(Integer, default=0)
    workshop_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workshop_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_by_discord_id: Mapped[str] = mapped_column(ID)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Denormalized, maintained when rounds open/skip. Not a played/unplayed flag.
    times_played: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ratings: Mapped[list["Rating"]] = relationship(back_populates="map", cascade="all, delete-orphan")

    @property
    def workshop_url(self) -> str:
        return f"https://steamcommunity.com/sharedfiles/filedetails/?id={self.workshop_id}"


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("map_id", "discord_user_id", name="uq_rating_map_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    map_id: Mapped[int] = mapped_column(ForeignKey("maps.id", ondelete="CASCADE"), index=True)
    discord_user_id: Mapped[str] = mapped_column(ID, index=True)
    stars: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Nullable: ratings can be given outside a session.
    round_id: Mapped[int | None] = mapped_column(ForeignKey("session_rounds.id", ondelete="SET NULL"))

    map: Mapped[Map] = relationship(back_populates="ratings")


class PlaySession(Base):
    __tablename__ = "play_sessions"
    # No uniqueness on guild_id: concurrent sessions per guild are supported.
    __table_args__ = (Index("ix_play_sessions_guild_status", "guild_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default=SessionStatus.ACTIVE)
    host_discord_id: Mapped[str] = mapped_column(ID)
    guild_id: Mapped[str] = mapped_column(ID, ForeignKey("guild_configs.guild_id"))
    # Filled in by the bot after it creates the thread.
    thread_id: Mapped[str | None] = mapped_column(ID)
    root_message_id: Mapped[str | None] = mapped_column(ID)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    players: Mapped[list["SessionPlayer"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="SessionPlayer.joined_at"
    )
    rounds: Mapped[list["SessionRound"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="SessionRound.round_number"
    )

    @property
    def active_players(self) -> list["SessionPlayer"]:
        return [p for p in self.players if p.left_at is None]

    @property
    def current_round(self) -> "SessionRound | None":
        for r in self.rounds:
            if r.status in RoundStatus.OPEN:
                return r
        return None


class SessionPlayer(Base):
    __tablename__ = "session_players"
    __table_args__ = (
        # One active session per user, enforced by the database. Relies on left_at
        # ALWAYS being set when a player exits (leave, session complete/cancel).
        Index(
            "uq_session_players_one_active",
            "discord_user_id",
            unique=True,
            postgresql_where=text("left_at IS NULL"),
            sqlite_where=text("left_at IS NULL"),
        ),
        Index("ix_session_players_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("play_sessions.id", ondelete="CASCADE"))
    discord_user_id: Mapped[str] = mapped_column(ID)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    session: Mapped[PlaySession] = relationship(back_populates="players")


class SessionRound(Base):
    __tablename__ = "session_rounds"
    __table_args__ = (
        UniqueConstraint("session_id", "round_number", name="uq_session_rounds_number"),
        # Only one open (downloading/playing) round per session.
        Index(
            "uq_session_rounds_one_open",
            "session_id",
            unique=True,
            postgresql_where=text("status NOT IN ('completed', 'skipped')"),
            sqlite_where=text("status NOT IN ('completed', 'skipped')"),
        ),
        Index("ix_session_rounds_map", "map_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("play_sessions.id", ondelete="CASCADE"))
    map_id: Mapped[int] = mapped_column(ForeignKey("maps.id"))
    round_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=RoundStatus.DOWNLOADING)
    # started_at = when the round was opened (map picked). This is also the value
    # that feeds Map.last_played_at.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The bot's map-card message in the session thread, edited in place.
    message_id: Mapped[str | None] = mapped_column(ID)

    session: Mapped[PlaySession] = relationship(back_populates="rounds")
    map: Mapped[Map] = relationship()
    players: Mapped[list["RoundPlayer"]] = relationship(
        back_populates="round", cascade="all, delete-orphan", order_by="RoundPlayer.id"
    )

    @property
    def ready_count(self) -> int:
        return sum(1 for p in self.players if p.ready_at is not None)


class RoundPlayer(Base):
    """Snapshot of who was in for a specific round. ready_at = "I've downloaded it"."""

    __tablename__ = "round_players"
    __table_args__ = (UniqueConstraint("round_id", "discord_user_id", name="uq_round_players_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("session_rounds.id", ondelete="CASCADE"))
    discord_user_id: Mapped[str] = mapped_column(ID)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    round: Mapped[SessionRound] = relationship(back_populates="players")


class Event(Base):
    """Outbox for the Discord bot. Written in the same transaction as the state
    change it describes; the bot polls undelivered rows and acks them."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_undelivered", "id", postgresql_where=text("delivered_at IS NULL")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("play_sessions.id", ondelete="SET NULL"))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
