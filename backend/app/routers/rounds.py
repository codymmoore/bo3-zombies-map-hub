from fastapi import APIRouter

from app import schemas
from app.deps import DbDep, MembershipDep, UserDep
from app.errors import bad_request
from app.routers.sessions import require_host_or_admin
from app.services import maps as map_service
from app.services import serialize
from app.services import sessions as session_service

router = APIRouter(prefix="/rounds", tags=["rounds"])


@router.patch("/{round_id}", response_model=schemas.RoundOut)
def patch_round(round_id: int, body: schemas.RoundPatch, db: DbDep, membership: MembershipDep, user: UserDep) -> schemas.RoundOut:
    """Host only: advance the round (playing / completed / skipped) or swap its map."""
    if body.status is None and body.map_id is None:
        raise bad_request("empty_patch", "Provide status and/or map_id")
    rnd = session_service.get_round_or_404(db, round_id)
    require_host_or_admin(membership, rnd.session, user.discord_id)
    if body.map_id is not None:
        m = map_service.get_map_or_404(db, body.map_id)
        rnd = session_service.swap_round_map(db, rnd, m)
    if body.status is not None:
        rnd = session_service.transition_round(db, rnd, body.status)
    return serialize.round_out(db, rnd, user.discord_id)


@router.post("/{round_id}/ready", response_model=schemas.RoundOut)
def set_ready(round_id: int, db: DbDep, user: UserDep, body: schemas.ReadyBody | None = None) -> schemas.RoundOut:
    """Caller marks themselves ready (downloaded). Advisory; the host can start regardless."""
    rnd = session_service.get_round_or_404(db, round_id)
    ready = body.ready if body is not None else True
    rnd = session_service.set_ready(db, rnd, user.discord_id, ready)
    return serialize.round_out(db, rnd, user.discord_id)
