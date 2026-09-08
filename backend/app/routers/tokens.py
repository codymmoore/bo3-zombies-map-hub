from fastapi import APIRouter
from sqlalchemy import select

from app import schemas
from app.auth.tokens import display_prefix, generate_token, hash_token
from app.deps import CookieUserDep, DbDep
from app.errors import not_found
from app.models import ApiToken, utcnow

router = APIRouter(prefix="/tokens", tags=["tokens"])


@router.get("", response_model=list[schemas.TokenOut])
def list_tokens(db: DbDep, user: CookieUserDep) -> list[schemas.TokenOut]:
    rows = db.scalars(
        select(ApiToken)
        .where(ApiToken.discord_user_id == user.discord_id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc())
    ).all()
    return [schemas.TokenOut.model_validate(r) for r in rows]


@router.post("", response_model=schemas.TokenCreated, status_code=201)
def create_token(body: schemas.TokenCreate, db: DbDep, user: CookieUserDep) -> schemas.TokenCreated:
    """The plaintext token is returned once and never stored."""
    token = generate_token()
    row = ApiToken(
        discord_user_id=user.discord_id,
        name=body.name,
        token_hash=hash_token(token),
        token_prefix=display_prefix(token),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return schemas.TokenCreated(
        id=row.id, name=row.name, token_prefix=row.token_prefix, created_at=row.created_at,
        last_used_at=row.last_used_at, token=token,
    )


@router.delete("/{token_id}", status_code=204)
def revoke_token(token_id: int, db: DbDep, user: CookieUserDep) -> None:
    row = db.get(ApiToken, token_id)
    if row is None or row.discord_user_id != user.discord_id or row.revoked_at is not None:
        raise not_found("Token")
    row.revoked_at = utcnow()
    db.commit()
