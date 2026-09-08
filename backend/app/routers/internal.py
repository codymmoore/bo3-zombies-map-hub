"""Endpoints for the Discord bot service, authenticated with X-Internal-Token.

The bot polls events, acts on them, then acks. Its cursor therefore lives in the
backend (delivered_at), so a bot restart resumes where it left off."""

from fastapi import APIRouter, Query
from sqlalchemy import select, update

from app import schemas
from app.deps import BotDep, DbDep
from app.models import Event, utcnow
from app.services import serialize
from app.services import sessions as session_service

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[BotDep])


@router.get("/events", response_model=list[schemas.EventOut])
def poll_events(db: DbDep, limit: int = Query(default=50, ge=1, le=200)) -> list[schemas.EventOut]:
    rows = db.scalars(select(Event).where(Event.delivered_at.is_(None)).order_by(Event.id).limit(limit)).all()
    return [schemas.EventOut.model_validate(r) for r in rows]


@router.post("/events/ack")
def ack_events(body: schemas.EventAck, db: DbDep) -> dict[str, int]:
    result = db.execute(
        update(Event).where(Event.id <= body.up_to_id, Event.delivered_at.is_(None)).values(delivered_at=utcnow())
    )
    db.commit()
    return {"acked": result.rowcount}


@router.get("/sessions/{session_id}", response_model=schemas.SessionDetail)
def get_session(session_id: int, db: DbDep) -> schemas.SessionDetail:
    """Current state, e.g. to recover thread_id after a bot restart."""
    session = session_service.get_session_or_404(db, session_id)
    return serialize.session_detail(db, session, None)


@router.patch("/rounds/{round_id}", response_model=schemas.RoundOut)
def patch_round(round_id: int, body: schemas.RoundInternalPatch, db: DbDep) -> schemas.RoundOut:
    """The bot stores the map-card message ID so edits survive a restart."""
    rnd = session_service.get_round_or_404(db, round_id)
    rnd.message_id = body.message_id
    db.commit()
    return serialize.round_out(db, rnd, None)
