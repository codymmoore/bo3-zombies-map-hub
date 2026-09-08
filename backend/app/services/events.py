"""Event outbox for the Discord bot.

Events are inserted in the same transaction as the state change they describe, so
they survive restarts and never describe state that was rolled back. The bot polls
`GET /internal/events`, acts on each one, and acks. Payloads are self-contained so
the bot never needs the database.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Event, GuildConfig, Map, PlaySession, Rating, SessionRound


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def session_payload(db: Session, session: PlaySession) -> dict:
    config = db.get(GuildConfig, session.guild_id)
    return {
        "id": session.id,
        "name": session.name,
        "status": session.status,
        "guild_id": session.guild_id,
        "notification_channel_id": config.notification_channel_id if config else None,
        "host_discord_id": session.host_discord_id,
        "thread_id": session.thread_id,
        "root_message_id": session.root_message_id,
        "started_at": _iso(session.started_at),
        "ended_at": _iso(session.ended_at),
        "players": [p.discord_user_id for p in session.active_players],
    }


def map_payload(db: Session, m: Map) -> dict:
    avg, count = db.execute(
        select(func.avg(Rating.stars), func.count(Rating.id)).where(Rating.map_id == m.id)
    ).one()
    return {
        "id": m.id,
        "workshop_id": m.workshop_id,
        "workshop_url": m.workshop_url,
        "title": m.title,
        "author_name": m.author_name,
        "thumbnail_url": m.thumbnail_url,
        "subscribers": m.subscribers,
        "times_played": m.times_played,
        "avg_rating": round(float(avg), 2) if avg is not None else None,
        "rating_count": int(count),
    }


def round_payload(db: Session, rnd: SessionRound) -> dict:
    return {
        "id": rnd.id,
        "session_id": rnd.session_id,
        "round_number": rnd.round_number,
        "status": rnd.status,
        "started_at": _iso(rnd.started_at),
        "ended_at": _iso(rnd.ended_at),
        "message_id": rnd.message_id,
        "map": map_payload(db, rnd.map),
        "players": [
            {"discord_user_id": p.discord_user_id, "ready": p.ready_at is not None} for p in rnd.players
        ],
        "ready_count": rnd.ready_count,
        "player_count": len(rnd.players),
    }


def emit(db: Session, event_type: str, session: PlaySession, payload: dict) -> Event:
    event = Event(type=event_type, session_id=session.id, payload=payload)
    db.add(event)
    return event


def emit_session_event(db: Session, event_type: str, session: PlaySession, **extra) -> Event:
    return emit(db, event_type, session, {"session": session_payload(db, session), **extra})


def emit_round_event(db: Session, event_type: str, rnd: SessionRound, **extra) -> Event:
    payload = {
        "session": session_payload(db, rnd.session),
        "round": round_payload(db, rnd),
        **extra,
    }
    return emit(db, event_type, rnd.session, payload)
