import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.errors import DiscordUnavailable, SteamNotFound, SteamUnavailable, SteamWrongApp
from app.routers import auth, guilds, internal, maps, rounds, sessions, tokens

log = logging.getLogger("app")


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": {"code": code, "message": message, **extra}})


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="BO3 Zombies Map Hub", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in (auth.router, maps.router, guilds.router, sessions.router, rounds.router, tokens.router, internal.router):
        app.include_router(router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(DiscordUnavailable)
    def _discord_unavailable(request: Request, exc: DiscordUnavailable) -> JSONResponse:
        log.warning("discord unavailable: %s", exc)
        return _error(503, "discord_unavailable", "Discord is unreachable right now, try again shortly")

    @app.exception_handler(SteamUnavailable)
    def _steam_unavailable(request: Request, exc: SteamUnavailable) -> JSONResponse:
        log.warning("steam unavailable: %s", exc)
        return _error(502, "steam_unavailable", "Steam is unreachable right now, try again shortly")

    @app.exception_handler(SteamNotFound)
    def _steam_not_found(request: Request, exc: SteamNotFound) -> JSONResponse:
        return _error(404, "workshop_item_not_found", "No public workshop item with that ID")

    @app.exception_handler(SteamWrongApp)
    def _steam_wrong_app(request: Request, exc: SteamWrongApp) -> JSONResponse:
        return _error(400, "wrong_app", "That workshop item is not a Black Ops III item", app_id=exc.app_id)

    @app.exception_handler(IntegrityError)
    def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        log.warning("integrity error: %s", exc.orig)
        return _error(409, "conflict", "That change conflicts with the current state, refresh and try again")

    return app


app = create_app()
