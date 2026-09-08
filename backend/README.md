# Backend

FastAPI + SQLAlchemy + Alembic + Postgres. See the root `CLAUDE.md` for the design.

## Run locally

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` elsewhere
pip install -e ".[dev]"
cp .env.example .env          # then fill in the Discord values
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --reload
```

API docs at http://localhost:8000/docs.

## Tests

```bash
pytest
```

Tests run against an in-memory SQLite database with the Discord and Steam clients
replaced by fakes, so they need no network or Postgres.

## Layout

| Path | What |
| --- | --- |
| `app/models.py` | SQLAlchemy models; the partial unique indexes from CLAUDE.md live here |
| `app/services/sessions.py` | Session/round lifecycle, all state transitions |
| `app/services/maps.py` | Map library: add from Steam, list/filter, ratings |
| `app/services/membership.py` | Guild membership/admin checks via the bot token, TTL cached |
| `app/services/steam.py` | Steam Web API client (the only Steam caller) |
| `app/services/events.py` | Event outbox the bot consumes |
| `app/routers/` | HTTP layer, one file per resource |
| `alembic/versions/` | Migrations |

## Authentication

- **Web app**: Discord OAuth2 (`identify` only). `GET /auth/discord/login` redirects to
  Discord; the callback sets an HTTP-only signed cookie and redirects to `FRONTEND_URL`.
- **Extension**: `POST /tokens` (browser login required) mints a long-lived token, sent
  as `Authorization: Bearer hub_...`.
- **Bot**: `X-Internal-Token: <INTERNAL_API_TOKEN>` on `/internal/*` and
  `PATCH /sessions/{id}`.

## Bot integration

The backend writes rows to the `events` table in the same transaction as the state
change. The bot loop is:

1. `GET /internal/events?limit=50` -> list of undelivered events, oldest first.
2. Act on each (post/edit Discord messages).
3. `POST /internal/events/ack {"up_to_id": <last id>}`.

Payloads are self-contained (`session`, `round`, `round.map` with rating aggregates,
`summary` on `session.ended`). After creating the thread the bot calls
`PATCH /sessions/{id}` with `thread_id` / `root_message_id`; after posting a map card it
calls `PATCH /internal/rounds/{id}` with `message_id`. `GET /internal/sessions/{id}`
returns current state for recovery after a restart.

## Error shape

Every error is `{"detail": {"code": "...", "message": "...", ...}}`. Codes the frontend
should branch on: `already_in_session` (includes `session_id`, `session_name`),
`not_a_member`, `host_required`, `admin_required`, `guild_not_configured`,
`discord_unavailable` (503, joins fail closed), `workshop_item_not_found`, `wrong_app`.
