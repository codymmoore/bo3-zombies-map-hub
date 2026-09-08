"""Session and round lifecycle. Every state change here commits together with
the outbox event that describes it, and always keeps SessionPlayer.left_at
consistent so the one-active-session-per-user index holds.

Host-leaves rule (chosen, applied consistently): promote the longest-tenured
remaining player; if nobody is left, cancel the session.
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.errors import bad_request, conflict, not_found
from app.models import (
    EventType,
    GuildConfig,
    Map,
    PlaySession,
    RoundPlayer,
    RoundStatus,
    SessionPlayer,
    SessionRound,
    SessionStatus,
    as_utc,
    utcnow,
)
from app.services import events

_SESSION_LOAD = (
    selectinload(PlaySession.players),
    selectinload(PlaySession.rounds).selectinload(SessionRound.map),
    selectinload(PlaySession.rounds).selectinload(SessionRound.players),
)


# -- lookups ------------------------------------------------------------------


def get_session(db: Session, session_id: int) -> PlaySession | None:
    return db.scalar(select(PlaySession).where(PlaySession.id == session_id).options(*_SESSION_LOAD))


def get_session_or_404(db: Session, session_id: int) -> PlaySession:
    session = get_session(db, session_id)
    if session is None:
        raise not_found("Session")
    return session


def get_round_or_404(db: Session, round_id: int) -> SessionRound:
    rnd = db.scalar(
        select(SessionRound)
        .where(SessionRound.id == round_id)
        .options(
            selectinload(SessionRound.map),
            selectinload(SessionRound.players),
            selectinload(SessionRound.session).options(*_SESSION_LOAD),
        )
    )
    if rnd is None:
        raise not_found("Round")
    return rnd


def list_sessions(db: Session, guild_id: str | None, status: str | None) -> list[PlaySession]:
    stmt = select(PlaySession).options(*_SESSION_LOAD).order_by(PlaySession.started_at.desc())
    if guild_id:
        stmt = stmt.where(PlaySession.guild_id == guild_id)
    if status:
        stmt = stmt.where(PlaySession.status == status)
    return list(db.scalars(stmt).all())


def active_membership(db: Session, discord_user_id: str) -> SessionPlayer | None:
    """The user's open SessionPlayer row, if any (at most one by construction)."""
    return db.scalar(
        select(SessionPlayer)
        .where(SessionPlayer.discord_user_id == discord_user_id, SessionPlayer.left_at.is_(None))
        .options(selectinload(SessionPlayer.session).options(*_SESSION_LOAD))
    )


def _already_in_session(existing: SessionPlayer):
    return conflict(
        "already_in_session",
        f"You're already in session \"{existing.session.name}\", leave it first",
        session_id=existing.session_id,
        session_name=existing.session.name,
    )


def _require_active(session: PlaySession) -> None:
    if session.status != SessionStatus.ACTIVE:
        raise conflict("session_not_active", "This session has ended")


# -- session lifecycle --------------------------------------------------------


def create_session(db: Session, host_discord_id: str, name: str, guild_id: str) -> PlaySession:
    if db.get(GuildConfig, guild_id) is None:
        raise bad_request("guild_not_configured", "This server has no notification channel configured yet")
    existing = active_membership(db, host_discord_id)
    if existing is not None:
        raise _already_in_session(existing)

    session = PlaySession(name=name, guild_id=guild_id, host_discord_id=host_discord_id)
    session.players.append(SessionPlayer(discord_user_id=host_discord_id))
    db.add(session)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = active_membership(db, host_discord_id)
        if existing is not None:
            raise _already_in_session(existing)
        raise
    events.emit_session_event(db, EventType.SESSION_CREATED, session)
    db.commit()
    return get_session_or_404(db, session.id)


def join_session(db: Session, session: PlaySession, discord_user_id: str) -> PlaySession:
    _require_active(session)
    existing = active_membership(db, discord_user_id)
    if existing is not None:
        if existing.session_id == session.id:
            return session  # idempotent
        raise _already_in_session(existing)

    session.players.append(SessionPlayer(discord_user_id=discord_user_id))
    current = session.current_round
    ready_changed = False
    # Late joiners only get onto the current round while it's still downloading.
    if current is not None and current.status == RoundStatus.DOWNLOADING:
        if not any(p.discord_user_id == discord_user_id for p in current.players):
            current.players.append(RoundPlayer(discord_user_id=discord_user_id))
            ready_changed = True
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = active_membership(db, discord_user_id)
        if existing is not None and existing.session_id != session.id:
            raise _already_in_session(existing)
        raise
    if ready_changed:
        events.emit_round_event(db, EventType.ROUND_READY_CHANGED, current)
    db.commit()
    return get_session_or_404(db, session.id)


def leave_session(db: Session, session: PlaySession, discord_user_id: str) -> PlaySession:
    _require_active(session)
    player = next((p for p in session.active_players if p.discord_user_id == discord_user_id), None)
    if player is None:
        raise conflict("not_in_session", "You're not in this session")

    now = utcnow()
    player.left_at = now

    # Drop them from the current round's snapshot only while it's still downloading;
    # once play started, the snapshot records who actually played.
    current = session.current_round
    ready_changed = False
    if current is not None and current.status == RoundStatus.DOWNLOADING:
        for rp in list(current.players):
            if rp.discord_user_id == discord_user_id:
                current.players.remove(rp)
                ready_changed = True

    if session.host_discord_id == discord_user_id:
        remaining = sorted(session.active_players, key=lambda p: (p.joined_at, p.id))
        if remaining:
            session.host_discord_id = remaining[0].discord_user_id
        else:
            _end_session(db, session, SessionStatus.CANCELLED, now)
            db.commit()
            return get_session_or_404(db, session.id)

    if ready_changed:
        events.emit_round_event(db, EventType.ROUND_READY_CHANGED, current)
    db.commit()
    return get_session_or_404(db, session.id)


def complete_session(db: Session, session: PlaySession, *, cancelled: bool = False) -> PlaySession:
    _require_active(session)
    _end_session(db, session, SessionStatus.CANCELLED if cancelled else SessionStatus.COMPLETED, utcnow())
    db.commit()
    return get_session_or_404(db, session.id)


def _end_session(db: Session, session: PlaySession, status: str, now) -> None:
    """Close the open round, set left_at on everyone, emit session.ended.
    Caller commits: this must land in the same transaction as the status change."""
    current = session.current_round
    if current is not None:
        _close_round(db, current, now, emit_event=True)
    for p in session.active_players:
        p.left_at = now
    session.status = status
    session.ended_at = now
    db.flush()

    played = [r for r in session.rounds if r.status == RoundStatus.COMPLETED]
    duration = (now - as_utc(session.started_at)).total_seconds()
    events.emit_session_event(
        db,
        EventType.SESSION_ENDED,
        session,
        summary={
            "cancelled": status == SessionStatus.CANCELLED,
            "duration_seconds": int(duration),
            "rounds_played": len(played),
            "maps_played": [
                {"round_number": r.round_number, "map_id": r.map_id, "title": r.map.title} for r in played
            ],
        },
    )


# -- rounds -------------------------------------------------------------------


def open_round(db: Session, session: PlaySession, m: Map) -> SessionRound:
    """Host picks a map: closes any open round, snapshots the roster, bumps play stats."""
    _require_active(session)
    now = utcnow()
    current = session.current_round
    if current is not None:
        _close_round(db, current, now, emit_event=True)

    next_number = (db.scalar(select(func.max(SessionRound.round_number)).where(SessionRound.session_id == session.id)) or 0) + 1
    rnd = SessionRound(session_id=session.id, map_id=m.id, round_number=next_number, started_at=now)
    for p in session.active_players:
        rnd.players.append(RoundPlayer(discord_user_id=p.discord_user_id))
    session.rounds.append(rnd)

    m.times_played += 1
    m.last_played_at = now
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("round_already_open", "Another round was opened concurrently, refresh and try again")
    rnd.map = m
    events.emit_round_event(db, EventType.ROUND_MAP_SELECTED, rnd)
    db.commit()
    return get_round_or_404(db, rnd.id)


def swap_round_map(db: Session, rnd: SessionRound, m: Map) -> SessionRound:
    """Change the map of a round that hasn't started yet. Ready flags reset."""
    _require_active(rnd.session)
    if rnd.status != RoundStatus.DOWNLOADING:
        raise conflict("round_not_downloading", "The map can only be swapped before the round starts")
    if m.id == rnd.map_id:
        return rnd
    now = utcnow()
    _rollback_play_stats(db, rnd.map, rnd)
    rnd.map = m
    rnd.map_id = m.id
    rnd.started_at = now
    for p in rnd.players:
        p.ready_at = None
    m.times_played += 1
    m.last_played_at = now
    db.flush()
    events.emit_round_event(db, EventType.ROUND_MAP_SELECTED, rnd, swapped=True)
    db.commit()
    return get_round_or_404(db, rnd.id)


def set_ready(db: Session, rnd: SessionRound, discord_user_id: str, ready: bool) -> SessionRound:
    _require_active(rnd.session)
    if rnd.status != RoundStatus.DOWNLOADING:
        raise conflict("round_not_downloading", "Ready-up is only open while the round is downloading")
    rp = next((p for p in rnd.players if p.discord_user_id == discord_user_id), None)
    if rp is None:
        raise conflict("not_in_round", "You're not part of this round")
    if (rp.ready_at is not None) == ready:
        return rnd  # no change
    rp.ready_at = utcnow() if ready else None
    db.flush()
    events.emit_round_event(db, EventType.ROUND_READY_CHANGED, rnd)
    db.commit()
    return get_round_or_404(db, rnd.id)


_TRANSITIONS: dict[str, set[str]] = {
    RoundStatus.DOWNLOADING: {RoundStatus.PLAYING, RoundStatus.SKIPPED},
    RoundStatus.PLAYING: {RoundStatus.COMPLETED, RoundStatus.SKIPPED},
}


def transition_round(db: Session, rnd: SessionRound, new_status: str) -> SessionRound:
    _require_active(rnd.session)
    allowed = _TRANSITIONS.get(rnd.status, set())
    if new_status not in allowed:
        raise conflict(
            "invalid_transition",
            f"Cannot move a round from {rnd.status} to {new_status}",
            allowed=sorted(allowed),
        )
    now = utcnow()
    if new_status == RoundStatus.PLAYING:
        rnd.status = RoundStatus.PLAYING
        db.flush()
        events.emit_round_event(db, EventType.ROUND_STARTED, rnd)
    else:
        _close_round(db, rnd, now, emit_event=True, status=new_status)
    db.commit()
    return get_round_or_404(db, rnd.id)


def _close_round(db: Session, rnd: SessionRound, now, *, emit_event: bool, status: str | None = None) -> None:
    """Close an open round. Auto-close rule: a round that never started is
    skipped (nobody played it, so play stats roll back); a playing round completes."""
    if status is None:
        status = RoundStatus.SKIPPED if rnd.status == RoundStatus.DOWNLOADING else RoundStatus.COMPLETED
    never_played = rnd.status == RoundStatus.DOWNLOADING
    rnd.status = status
    rnd.ended_at = now
    if status == RoundStatus.SKIPPED and never_played:
        _rollback_play_stats(db, rnd.map, rnd)
    db.flush()
    if emit_event:
        events.emit_round_event(db, EventType.ROUND_ENDED, rnd)


def _rollback_play_stats(db: Session, m: Map, rnd: SessionRound) -> None:
    """Undo the increment done when the round opened, recomputing last_played_at
    from the map's other non-skipped rounds."""
    m.times_played = max(0, m.times_played - 1)
    m.last_played_at = db.scalar(
        select(func.max(SessionRound.started_at)).where(
            SessionRound.map_id == m.id,
            SessionRound.id != rnd.id,
            SessionRound.status != RoundStatus.SKIPPED,
        )
    )
