from fastapi import APIRouter, Query

from app import schemas
from app.deps import BotDep, DbDep, MembershipDep, UserDep
from app.errors import forbidden
from app.models import PlaySession
from app.services import maps as map_service
from app.services import serialize
from app.services import sessions as session_service
from app.services.membership import MembershipService

router = APIRouter(prefix="/sessions", tags=["sessions"])


def require_member(membership: MembershipService, guild_id: str, user_id: str) -> None:
    """Every session endpoint goes through here. DiscordUnavailable propagates
    to the global handler (503) so joins fail closed."""
    if not membership.is_member(guild_id, user_id):
        raise forbidden("You're not a member of this server", code="not_a_member")


def require_host_or_admin(membership: MembershipService, session: PlaySession, user_id: str) -> None:
    if session.host_discord_id == user_id:
        return
    if membership.is_admin(session.guild_id, user_id):
        return
    raise forbidden("Only the host or a server admin can do that", code="host_required")


@router.get("", response_model=list[schemas.SessionOut])
def list_sessions(
    db: DbDep,
    membership: MembershipDep,
    user: UserDep,
    guild_id: str = Query(min_length=1, max_length=32),
    status: str | None = Query(default=None, pattern="^(active|completed|cancelled)$"),
) -> list[schemas.SessionOut]:
    require_member(membership, guild_id, user.discord_id)
    return [serialize.session_out(s) for s in session_service.list_sessions(db, guild_id, status)]


@router.get("/mine", response_model=schemas.SessionDetail | None)
def my_session(db: DbDep, user: UserDep) -> schemas.SessionDetail | None:
    membership_row = session_service.active_membership(db, user.discord_id)
    if membership_row is None:
        return None
    return serialize.session_detail(db, membership_row.session, user.discord_id)


@router.post("", response_model=schemas.SessionDetail, status_code=201)
def create_session(body: schemas.SessionCreate, db: DbDep, membership: MembershipDep, user: UserDep) -> schemas.SessionDetail:
    require_member(membership, body.guild_id, user.discord_id)
    session = session_service.create_session(db, user.discord_id, body.name, body.guild_id)
    return serialize.session_detail(db, session, user.discord_id)


@router.get("/{session_id}", response_model=schemas.SessionDetail)
def get_session(session_id: int, db: DbDep, membership: MembershipDep, user: UserDep) -> schemas.SessionDetail:
    session = session_service.get_session_or_404(db, session_id)
    require_member(membership, session.guild_id, user.discord_id)
    return serialize.session_detail(db, session, user.discord_id)


@router.patch("/{session_id}", response_model=schemas.SessionOut, dependencies=[BotDep])
def patch_session_internal(session_id: int, body: schemas.SessionInternalPatch, db: DbDep) -> schemas.SessionOut:
    """Internal: the bot reports the thread/root message it created."""
    session = session_service.get_session_or_404(db, session_id)
    if body.thread_id is not None:
        session.thread_id = body.thread_id
    if body.root_message_id is not None:
        session.root_message_id = body.root_message_id
    db.commit()
    return serialize.session_out(session)


@router.post("/{session_id}/players", response_model=schemas.SessionDetail)
def join_session(session_id: int, db: DbDep, membership: MembershipDep, user: UserDep) -> schemas.SessionDetail:
    session = session_service.get_session_or_404(db, session_id)
    require_member(membership, session.guild_id, user.discord_id)
    session = session_service.join_session(db, session, user.discord_id)
    return serialize.session_detail(db, session, user.discord_id)


@router.delete("/{session_id}/players/me", response_model=schemas.SessionDetail)
def leave_session(session_id: int, db: DbDep, user: UserDep) -> schemas.SessionDetail:
    """Deliberately no membership check: someone removed from the Discord server
    must still be able to leave, or they'd be stuck in an active session forever."""
    session = session_service.get_session_or_404(db, session_id)
    session = session_service.leave_session(db, session, user.discord_id)
    return serialize.session_detail(db, session, user.discord_id)


@router.post("/{session_id}/complete", response_model=schemas.SessionDetail)
def complete_session(session_id: int, db: DbDep, membership: MembershipDep, user: UserDep) -> schemas.SessionDetail:
    session = session_service.get_session_or_404(db, session_id)
    require_host_or_admin(membership, session, user.discord_id)
    session = session_service.complete_session(db, session)
    return serialize.session_detail(db, session, user.discord_id)


@router.post("/{session_id}/cancel", response_model=schemas.SessionDetail)
def cancel_session(session_id: int, db: DbDep, membership: MembershipDep, user: UserDep) -> schemas.SessionDetail:
    session = session_service.get_session_or_404(db, session_id)
    require_host_or_admin(membership, session, user.discord_id)
    session = session_service.complete_session(db, session, cancelled=True)
    return serialize.session_detail(db, session, user.discord_id)


@router.post("/{session_id}/rounds", response_model=schemas.RoundOut, status_code=201)
def open_round(
    session_id: int, body: schemas.RoundCreate, db: DbDep, membership: MembershipDep, user: UserDep
) -> schemas.RoundOut:
    """Host picks a map. Any open round is auto-closed first."""
    session = session_service.get_session_or_404(db, session_id)
    require_host_or_admin(membership, session, user.discord_id)
    m = map_service.get_map_or_404(db, body.map_id)
    rnd = session_service.open_round(db, session, m)
    return serialize.round_out(db, rnd, user.discord_id)


@router.get("/{session_id}/rounds", response_model=list[schemas.RoundOut])
def list_rounds(session_id: int, db: DbDep, membership: MembershipDep, user: UserDep) -> list[schemas.RoundOut]:
    session = session_service.get_session_or_404(db, session_id)
    require_member(membership, session.guild_id, user.discord_id)
    return serialize.rounds_out(db, session.rounds, user.discord_id)
