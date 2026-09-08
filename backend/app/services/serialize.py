"""Model -> response schema conversion, with the aggregate lookups batched."""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.models import DiscordUser, Map, PlaySession, SessionRound
from app.services.maps import my_ratings, rating_stats


def map_out(db: Session, m: Map, viewer_id: str | None) -> schemas.MapOut:
    return maps_out(db, [m], viewer_id)[0]


def maps_out(db: Session, maps: list[Map], viewer_id: str | None) -> list[schemas.MapOut]:
    ids = [m.id for m in maps]
    stats = rating_stats(db, ids)
    mine = my_ratings(db, ids, viewer_id)
    out = []
    for m in maps:
        avg, count = stats.get(m.id, (None, 0))
        item = schemas.MapOut.model_validate(m)
        item.avg_rating = avg
        item.rating_count = count
        item.my_rating = mine.get(m.id)
        out.append(item)
    return out


def _users(db: Session, ids: Iterable[str]) -> dict[str, schemas.UserOut]:
    unique = list({i for i in ids if i})
    if not unique:
        return {}
    rows = db.scalars(select(DiscordUser).where(DiscordUser.discord_id.in_(unique))).all()
    return {u.discord_id: schemas.UserOut.model_validate(u) for u in rows}


def round_out(db: Session, rnd: SessionRound, viewer_id: str | None) -> schemas.RoundOut:
    users = _users(db, (p.discord_user_id for p in rnd.players))
    return _round_out(db, rnd, viewer_id, users)


def _round_out(db: Session, rnd: SessionRound, viewer_id: str | None, users: dict[str, schemas.UserOut]) -> schemas.RoundOut:
    return schemas.RoundOut(
        id=rnd.id,
        session_id=rnd.session_id,
        round_number=rnd.round_number,
        status=rnd.status,
        map=map_out(db, rnd.map, viewer_id),
        started_at=rnd.started_at,
        ended_at=rnd.ended_at,
        message_id=rnd.message_id,
        players=[
            schemas.RoundPlayerOut(discord_user_id=p.discord_user_id, user=users.get(p.discord_user_id), ready_at=p.ready_at)
            for p in rnd.players
        ],
        ready_count=rnd.ready_count,
        player_count=len(rnd.players),
    )


def session_out(session: PlaySession) -> schemas.SessionOut:
    current = session.current_round
    return schemas.SessionOut(
        id=session.id,
        name=session.name,
        status=session.status,
        host_discord_id=session.host_discord_id,
        guild_id=session.guild_id,
        thread_id=session.thread_id,
        root_message_id=session.root_message_id,
        started_at=session.started_at,
        ended_at=session.ended_at,
        player_count=len(session.active_players),
        current_round_number=current.round_number if current else None,
    )


def session_detail(db: Session, session: PlaySession, viewer_id: str | None) -> schemas.SessionDetail:
    ids = {p.discord_user_id for p in session.players}
    for r in session.rounds:
        ids.update(p.discord_user_id for p in r.players)
    users = _users(db, ids)

    rounds = [_round_out(db, r, viewer_id, users) for r in session.rounds]
    current = session.current_round
    base = session_out(session)
    return schemas.SessionDetail(
        **base.model_dump(),
        players=[
            schemas.SessionPlayerOut(
                discord_user_id=p.discord_user_id,
                user=users.get(p.discord_user_id),
                joined_at=p.joined_at,
                is_host=p.discord_user_id == session.host_discord_id,
            )
            for p in session.active_players
        ],
        current_round=next((r for r in rounds if current and r.id == current.id), None),
        rounds=rounds,
    )
