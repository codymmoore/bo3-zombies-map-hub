"""Map library: adding from Steam, listing/filtering, ratings."""

from collections.abc import Iterable
from typing import Literal

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError, bad_request, not_found
from app.models import Map, Rating, RoundPlayer, SessionRound
from app.services.steam import SteamClient, parse_workshop_id

SortKey = Literal["subscribers", "rating", "created_at", "times_played", "title", "added_at"]


def rating_stats(db: Session, map_ids: Iterable[int]) -> dict[int, tuple[float | None, int]]:
    ids = list(map_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(Rating.map_id, func.avg(Rating.stars), func.count(Rating.id))
        .where(Rating.map_id.in_(ids))
        .group_by(Rating.map_id)
    ).all()
    return {map_id: (round(float(avg), 2), int(count)) for map_id, avg, count in rows}


def my_ratings(db: Session, map_ids: Iterable[int], discord_user_id: str | None) -> dict[int, int]:
    ids = list(map_ids)
    if not ids or discord_user_id is None:
        return {}
    rows = db.execute(
        select(Rating.map_id, Rating.stars).where(Rating.map_id.in_(ids), Rating.discord_user_id == discord_user_id)
    ).all()
    return {map_id: stars for map_id, stars in rows}


def list_maps(
    db: Session,
    *,
    author: str | None = None,
    q: str | None = None,
    sort: SortKey = "added_at",
    order: Literal["asc", "desc"] = "desc",
    unrated_for: list[str] | None = None,
    page: int = 1,
    page_size: int = 24,
) -> tuple[list[Map], int]:
    """`unrated_for`: restrict to maps that NONE of these users has rated."""
    stats = (
        select(
            Rating.map_id.label("map_id"),
            func.avg(Rating.stars).label("avg_rating"),
            func.count(Rating.id).label("rating_count"),
        )
        .group_by(Rating.map_id)
        .subquery()
    )

    base = select(Map).outerjoin(stats, stats.c.map_id == Map.id)

    if author:
        base = base.where(or_(Map.author_steam_id == author, func.lower(Map.author_name) == author.lower()))
    if q:
        pattern = f"%{q.strip()}%"
        base = base.where(or_(Map.title.ilike(pattern), Map.author_name.ilike(pattern)))
    if unrated_for:
        base = base.where(
            ~exists().where(Rating.map_id == Map.id, Rating.discord_user_id.in_(unrated_for))
        )

    total = db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0

    sort_columns = {
        "subscribers": Map.subscribers,
        "rating": stats.c.avg_rating,
        "created_at": Map.workshop_created_at,
        "times_played": Map.times_played,
        "title": func.lower(Map.title),
        "added_at": Map.added_at,
    }
    col = sort_columns[sort]
    primary = col.desc().nulls_last() if order == "desc" else col.asc().nulls_last()
    items = list(
        db.scalars(base.order_by(primary, Map.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    )
    return items, int(total)


def lookup_by_workshop_ids(db: Session, workshop_ids: Iterable[str]) -> dict[str, int]:
    ids = [w for w in workshop_ids if w]
    if not ids:
        return {}
    rows = db.execute(select(Map.workshop_id, Map.id).where(Map.workshop_id.in_(ids))).all()
    return {workshop_id: map_id for workshop_id, map_id in rows}


def add_map(db: Session, steam: SteamClient, workshop: str, added_by: str) -> tuple[Map, bool]:
    """Returns (map, created). Re-adding an existing map returns it unchanged."""
    workshop_id = parse_workshop_id(workshop)
    if workshop_id is None:
        raise bad_request("invalid_workshop_ref", "Expected a Steam Workshop item ID or URL")

    existing = db.scalar(select(Map).where(Map.workshop_id == workshop_id))
    if existing is not None:
        return existing, False

    item = steam.fetch_item(workshop_id)
    m = Map(
        workshop_id=item.workshop_id,
        title=item.title[:255],
        author_steam_id=item.author_steam_id,
        author_name=item.author_name[:255] if item.author_name else None,
        thumbnail_url=item.thumbnail_url,
        description=item.description,
        subscribers=item.subscribers,
        workshop_created_at=item.created_at,
        workshop_updated_at=item.updated_at,
        added_by_discord_id=added_by,
    )
    db.add(m)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent add of the same item.
        db.rollback()
        existing = db.scalar(select(Map).where(Map.workshop_id == workshop_id))
        if existing is None:
            raise
        return existing, False
    db.refresh(m)
    return m, True


def get_map_or_404(db: Session, map_id: int) -> Map:
    m = db.get(Map, map_id)
    if m is None:
        raise not_found("Map")
    return m


def rate_map(db: Session, m: Map, discord_user_id: str, stars: int, round_id: int | None) -> Rating:
    """One rating per user per MAP (upsert). round_id is optional provenance."""
    if round_id is not None:
        rnd = db.get(SessionRound, round_id)
        if rnd is None:
            raise not_found("Round")
        if rnd.map_id != m.id:
            raise bad_request("round_map_mismatch", "That round was not played on this map")
        was_in_round = db.scalar(
            select(exists().where(RoundPlayer.round_id == round_id, RoundPlayer.discord_user_id == discord_user_id))
        )
        if not was_in_round:
            raise ApiError(403, "not_in_round", "You were not part of that round")

    rating = db.scalar(select(Rating).where(Rating.map_id == m.id, Rating.discord_user_id == discord_user_id))
    if rating is None:
        rating = Rating(map_id=m.id, discord_user_id=discord_user_id, stars=stars, round_id=round_id)
        db.add(rating)
    else:
        rating.stars = stars
        if round_id is not None:
            rating.round_id = round_id
    db.commit()
    db.refresh(rating)
    return rating
