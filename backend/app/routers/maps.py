from typing import Literal

from fastapi import APIRouter, Query, Response

from app import schemas
from app.deps import DbDep, OptionalUserDep, SteamDep, UserDep
from app.errors import ApiError, forbidden
from app.services import maps as map_service
from app.services import serialize, sessions as session_service

router = APIRouter(prefix="/maps", tags=["maps"])

LOOKUP_MAX_IDS = 200


@router.post("", response_model=schemas.MapOut, status_code=201)
def add_map(body: schemas.MapCreate, db: DbDep, steam: SteamDep, user: UserDep, response: Response) -> schemas.MapOut:
    """Add a map by workshop ID or URL. Returns 200 (not 201) if it was already in the library."""
    m, created = map_service.add_map(db, steam, body.workshop, user.discord_id)
    if not created:
        response.status_code = 200
    return serialize.map_out(db, m, user.discord_id)


@router.get("/lookup", response_model=schemas.MapLookupOut)
def lookup_maps(
    db: DbDep,
    user: UserDep,
    workshop_ids: str = Query(description="Comma-separated workshop IDs", max_length=4000),
) -> schemas.MapLookupOut:
    """Batch check which workshop IDs are already in the library (for the extension).
    Max 200 per call; the extension chunks larger grids."""
    ids = [w.strip() for w in workshop_ids.split(",") if w.strip()]
    if len(ids) > LOOKUP_MAX_IDS:
        raise ApiError(400, "too_many_ids", f"At most {LOOKUP_MAX_IDS} workshop IDs per lookup", max=LOOKUP_MAX_IDS)
    return schemas.MapLookupOut(found=map_service.lookup_by_workshop_ids(db, ids))


@router.get("", response_model=schemas.MapPage)
def list_maps(
    db: DbDep,
    viewer: OptionalUserDep,
    author: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    sort: map_service.SortKey = "added_at",
    order: Literal["asc", "desc"] = "desc",
    unrated: bool = False,
    unrated_by_session: int | None = Query(default=None, description="Extend `unrated` to a session's whole roster"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
) -> schemas.MapPage:
    viewer_id = viewer.discord_id if viewer else None
    unrated_for: list[str] | None = None
    if unrated or unrated_by_session is not None:
        if viewer is None:
            raise ApiError(401, "unauthenticated", "Login required for the unrated filter")
        unrated_for = [viewer.discord_id]
        if unrated_by_session is not None:
            session = session_service.get_session_or_404(db, unrated_by_session)
            roster = [p.discord_user_id for p in session.active_players]
            if viewer.discord_id not in roster and viewer.discord_id != session.host_discord_id:
                raise forbidden("You're not in that session")
            unrated_for = sorted(set(roster) | {viewer.discord_id})

    items, total = map_service.list_maps(
        db, author=author, q=q, sort=sort, order=order, unrated_for=unrated_for, page=page, page_size=page_size
    )
    return schemas.MapPage(items=serialize.maps_out(db, items, viewer_id), total=total, page=page, page_size=page_size)


@router.get("/{map_id}", response_model=schemas.MapOut)
def get_map(map_id: int, db: DbDep, viewer: OptionalUserDep) -> schemas.MapOut:
    m = map_service.get_map_or_404(db, map_id)
    return serialize.map_out(db, m, viewer.discord_id if viewer else None)


@router.post("/{map_id}/ratings", response_model=schemas.RatingOut, status_code=201)
def rate_map(map_id: int, body: schemas.RatingCreate, db: DbDep, user: UserDep) -> schemas.RatingOut:
    m = map_service.get_map_or_404(db, map_id)
    rating = map_service.rate_map(db, m, user.discord_id, body.stars, body.round_id)
    return schemas.RatingOut.model_validate(rating)
