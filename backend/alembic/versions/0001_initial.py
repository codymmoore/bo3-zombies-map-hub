"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ID = sa.String(length=32)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "discord_users",
        sa.Column("discord_id", ID, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("global_name", sa.String(length=64), nullable=True),
        sa.Column("avatar", sa.String(length=64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_login_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("discord_id"),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_user_id", ID, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["discord_user_id"], ["discord_users.discord_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_api_tokens_discord_user_id", "api_tokens", ["discord_user_id"])

    op.create_table(
        "guild_configs",
        sa.Column("guild_id", ID, nullable=False),
        sa.Column("notification_channel_id", ID, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("guild_id"),
    )

    op.create_table(
        "maps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workshop_id", ID, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("author_steam_id", ID, nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("subscribers", sa.Integer(), nullable=False),
        sa.Column("workshop_created_at", TS, nullable=True),
        sa.Column("workshop_updated_at", TS, nullable=True),
        sa.Column("added_by_discord_id", ID, nullable=False),
        sa.Column("added_at", TS, nullable=False),
        sa.Column("times_played", sa.Integer(), nullable=False),
        sa.Column("last_played_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workshop_id"),
    )

    op.create_table(
        "play_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("host_discord_id", ID, nullable=False),
        sa.Column("guild_id", ID, nullable=False),
        sa.Column("thread_id", ID, nullable=True),
        sa.Column("root_message_id", ID, nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("ended_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["guild_id"], ["guild_configs.guild_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # No uniqueness on guild_id: concurrent sessions per guild are supported.
    op.create_index("ix_play_sessions_guild_status", "play_sessions", ["guild_id", "status"])

    op.create_table(
        "session_players",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("discord_user_id", ID, nullable=False),
        sa.Column("joined_at", TS, nullable=False),
        sa.Column("left_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["play_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # One active session per user, enforced in the database.
    op.create_index(
        "uq_session_players_one_active",
        "session_players",
        ["discord_user_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL"),
    )
    op.create_index("ix_session_players_session", "session_players", ["session_id"])

    op.create_table(
        "session_rounds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("map_id", sa.Integer(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("message_id", ID, nullable=True),
        sa.ForeignKeyConstraint(["map_id"], ["maps.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["play_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "round_number", name="uq_session_rounds_number"),
    )
    # Only one open (downloading/playing) round per session.
    op.create_index(
        "uq_session_rounds_one_open",
        "session_rounds",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('completed', 'skipped')"),
    )
    op.create_index("ix_session_rounds_map", "session_rounds", ["map_id"])

    op.create_table(
        "round_players",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("discord_user_id", ID, nullable=False),
        sa.Column("ready_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["round_id"], ["session_rounds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("round_id", "discord_user_id", name="uq_round_players_user"),
    )

    op.create_table(
        "ratings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("map_id", sa.Integer(), nullable=False),
        sa.Column("discord_user_id", ID, nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["map_id"], ["maps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["session_rounds.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("map_id", "discord_user_id", name="uq_rating_map_user"),
    )
    op.create_index("ix_ratings_map_id", "ratings", ["map_id"])
    op.create_index("ix_ratings_discord_user_id", "ratings", ["discord_user_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("delivered_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["play_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_undelivered", "events", ["id"], postgresql_where=sa.text("delivered_at IS NULL"))


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("ratings")
    op.drop_table("round_players")
    op.drop_table("session_rounds")
    op.drop_table("session_players")
    op.drop_table("play_sessions")
    op.drop_table("maps")
    op.drop_table("guild_configs")
    op.drop_table("api_tokens")
    op.drop_table("discord_users")
