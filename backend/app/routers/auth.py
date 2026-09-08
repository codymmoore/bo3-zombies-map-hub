import secrets
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app import schemas
from app.auth.cookies import issue_session_token, issue_state_token, read_state_token
from app.deps import DbDep, OAuthDep, SettingsDep, UserDep
from app.errors import bad_request
from app.models import DiscordUser, utcnow

router = APIRouter(prefix="/auth", tags=["auth"])

NONCE_COOKIE = "oauth_nonce"


def _safe_next(value: str | None) -> str:
    """Only allow same-site relative paths as post-login destinations."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


@router.get("/discord/login")
def discord_login(settings: SettingsDep, oauth: OAuthDep, next: str | None = None) -> RedirectResponse:
    nonce = secrets.token_urlsafe(16)
    state = issue_state_token(settings.secret_key, _safe_next(next), nonce)
    resp = RedirectResponse(oauth.authorize_url(state), status_code=302)
    resp.set_cookie(NONCE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax", secure=settings.cookie_secure)
    return resp


@router.get("/discord/callback")
def discord_callback(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    oauth: OAuthDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"{settings.frontend_url}/?login_error={error}", status_code=302)
    if not code or not state:
        raise bad_request("invalid_callback", "Missing code or state")
    claims = read_state_token(settings.secret_key, state)
    nonce = request.cookies.get(NONCE_COOKIE)
    if claims is None or not nonce or not secrets.compare_digest(claims.get("nonce", ""), nonce):
        raise bad_request("invalid_state", "OAuth state did not validate, try logging in again")

    identity = oauth.exchange_code(code)

    user = db.get(DiscordUser, identity.discord_id)
    if user is None:
        user = DiscordUser(discord_id=identity.discord_id)
        db.add(user)
    user.username = identity.username
    user.global_name = identity.global_name
    user.avatar = identity.avatar
    user.last_login_at = utcnow()
    db.commit()

    max_age = timedelta(days=settings.session_max_age_days)
    token = issue_session_token(settings.secret_key, user.discord_id, max_age)
    resp = RedirectResponse(f"{settings.frontend_url}{_safe_next(claims.get('next'))}", status_code=302)
    resp.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=int(max_age.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    resp.delete_cookie(NONCE_COOKIE)
    return resp


@router.post("/logout", status_code=204)
def logout(response: Response, settings: SettingsDep) -> None:
    """Clears the cookie only. Sessions are stateless JWTs, so a copied cookie stays
    valid until it expires; that's a deliberate MVP trade-off (no session table)."""
    response.delete_cookie(settings.session_cookie_name)


@router.get("/me", response_model=schemas.MeOut)
def me(user: UserDep) -> schemas.MeOut:
    return schemas.MeOut(
        discord_id=user.user.discord_id,
        username=user.user.username,
        display_name=user.user.display_name,
        avatar_url=user.user.avatar_url,
        auth_via=user.via,
    )
