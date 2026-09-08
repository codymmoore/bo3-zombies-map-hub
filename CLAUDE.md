# Project context

A Black Ops 3 custom zombies map manager. Users add Steam Workshop maps to a shared
library, browse/search them, run play sessions, and rate maps afterward.

**The web UI is the application.** All user actions happen there. The Discord bot is a
notification service only — it posts updates into a thread and has no commands. The
Chrome extension is a shortcut for adding maps to the library.

**Scope note:** this is an MVP. The session host picks each map directly. Group voting
is deliberately deferred (see "Deferred"). Don't add voting unless asked.

## Stack

- **Backend**: FastAPI (Python), SQLAlchemy + Alembic, Postgres
- **Frontend**: SvelteKit + TypeScript
- **Bot**: discord.py, runs as a separate service
- **Auth**: Discord OAuth2 (`identify` scope only) for the web app; long-lived
  per-user API tokens for the extension. Guild membership is checked server-side with
  the bot token — see "Auth and scoping".

## Architectural rules

These are decisions already made. Don't relitigate them without asking.

1. **The web UI is the only place actions are taken.** Don't add slash commands. If a
   feature seems to need one, it belongs in the UI instead.
2. **The bot is write-only toward Discord.** It consumes events from the backend and
   posts/edits messages. It does not accept user input, and it never touches the
   database directly.
3. **The backend owns all Steam API calls.** Clients send a workshop ID (or a URL to
   parse); the backend fetches metadata from
   `ISteamRemoteStorage/GetPublishedFileDetails/v1/`. No client-side scraping of
   Steam metadata. This is what keeps the Chrome extension trivially small.
4. **Session state lives in the backend**, not in-memory in the bot. Must survive a
   process restart.
5. **A session is a container for an ordered sequence of rounds.** One round = one map.
   Nearly everything map-related (ratings, play counts, who was present) attaches to the
   round, not the session.
6. **Multiple sessions can be active in the same Discord server at once.** Nothing may
   assume one-session-per-guild. Each session gets its own Discord thread.
7. **A user may be in only one active session at a time.** Enforced in the database, not
   just in application code — see `SessionPlayer`.

## Auth and scoping

- Login is Discord OAuth2 with the **`identify` scope only**. It establishes who the
  user is; it says nothing about what servers they're in. Do not add the `guilds` scope.
- **Membership is checked server-side using the bot token**, via
  `GET /guilds/{guild_id}/members/{user_id}` on Discord's REST API. 404 means not a
  member. This is authoritative and always current, unlike a guild list cached from the
  user's OAuth token, and it returns the user's roles — which the `guilds` scope cannot.
- Put this behind one `membership.is_member(guild_id, user_id)` helper with a short TTL
  cache (a minute or two is plenty — it's for rate limits, not correctness). Every
  session endpoint calls it against `session.guild_id`; nothing else talks to that
  Discord endpoint.
- The membership lookup lives in the **backend**, which holds the bot token for REST
  calls. The bot service is still the only thing with a gateway connection and the only
  thing that posts messages. Rule 2 is about message-sending, not about the token.
- If Discord is unreachable, fail closed on joins (deny with a clear error) but serve
  cached answers for reads. Don't hard-fail the whole app on a Discord outage.
- **Sessions are guild-scoped.** Any member of a session's guild may join it; there is
  no invite, approval step, or per-session privacy.
- The **map library is global**, shared across all guilds. (Flag this if it becomes a
  problem — per-guild libraries would mean adding `guild_id` to `Map` and to every
  ratings aggregate.)

## Data model

```
GuildConfig
  guild_id (PK), notification_channel_id, created_at
  # one row per Discord server; the channel where session threads are created.
  # Set during setup. Sessions cannot be created in a guild without this row.

Map
  id, workshop_id (unique), title, author_steam_id, author_name,
  thumbnail_url, description, subscribers,
  workshop_created_at, workshop_updated_at,
  added_by_discord_id, added_at,
  times_played, last_played_at   # denormalized, updated when a round starts.
                                 # NOT a played/unplayed flag — see GET /maps.

Rating
  id, map_id -> Map, discord_user_id, stars (1-5), created_at,
  round_id -> SessionRound (nullable — ratings can be given outside a session)
  UNIQUE (map_id, discord_user_id)   # upsert on re-rate; one rating per user per MAP,
                                     # not per round. Replaying updates the rating.

PlaySession
  id, name, status, host_discord_id, guild_id -> GuildConfig,
  thread_id (nullable), root_message_id (nullable),
  started_at, ended_at
  # NO uniqueness on guild_id — concurrent sessions are supported.
  # Index (guild_id, status) for "active sessions in this server".
  # thread_id/root_message_id are filled in by the bot after it creates the thread.

SessionPlayer
  id, session_id -> PlaySession, discord_user_id, joined_at, left_at (nullable)
  # PARTIAL UNIQUE INDEX on (discord_user_id) WHERE left_at IS NULL
  #   -> enforces one active session per user.
  # This only works if left_at is ALWAYS set when a player exits: on explicit leave,
  # and for every remaining player when a session is completed or cancelled.
  # Do that in the same transaction as the status change.

SessionRound
  id, session_id -> PlaySession, map_id -> Map, round_number,
  status, started_at, playing_started_at (nullable), ended_at, message_id (nullable)
  # started_at = when the map was picked; playing_started_at = downloading -> playing.
  # A skipped round with playing_started_at set was actually played and keeps
  # counting toward times_played / last_played_at.
  # partial unique index on session_id WHERE status != 'completed'
  #   -> only one open round per session
  # UNIQUE (session_id, round_number)

RoundPlayer
  id, round_id -> SessionRound, discord_user_id, ready_at (nullable)
  # snapshot of who was in for THIS round; ready_at = "I've downloaded it"
  # UNIQUE (round_id, discord_user_id)
```

## Session lifecycle

Session status: `active` -> `completed` (plus `cancelled`)
Round status: `downloading` -> `playing` -> `completed` (plus `skipped`)

All transitions are triggered from the web UI. The bot mirrors them into Discord.

1. Host creates a session in the UI. Backend emits `session.created`; the bot posts a
   root message in the guild's `notification_channel_id`, creates a thread from it, and
   reports the thread and message IDs back to the backend. Every later update for this
   session goes in that thread.
2. Players open the link and join. Creates `SessionPlayer` rows. Reject the join if the
   user already has an open `SessionPlayer` elsewhere — surface "you're already in
   session X, leave it first" in the UI rather than a raw constraint error.
3. Host picks a map from the library UI. Creates a `SessionRound` in `downloading`,
   snapshots the current roster into `RoundPlayer`, increments `times_played` and sets
   `last_played_at`. Bot posts a map card in the thread: title, thumbnail, average
   rating, and the workshop link so players can subscribe.
4. Players click "Ready" in the UI once downloaded — sets `ready_at`. The bot edits the
   map card to show an X/Y ready count. Advisory, not a gate; the host can start anyway.
5. Host starts the round -> `playing`.
6. Host ends the round -> `completed`. The UI surfaces a rating prompt to everyone in
   `RoundPlayer` (not the whole roster). Ratings stay open afterward — don't block the
   next round on them.
7. Back to step 3 for the next map. Loop until the host ends the session, which sets
   `left_at` on all remaining players and archives the thread.

Rules:
- Only the host (or a server admin) may pick maps, advance rounds, or end the session.
- Starting a new round auto-completes any open one.
- Players joining mid-session are added to `SessionPlayer`, and to `RoundPlayer` for the
  current round only if it's still `downloading`.
- Skipping a round marks it `skipped` — decrement `times_played` and roll back
  `last_played_at` if nobody actually played it.
- If the host leaves, either promote the longest-tenured player or auto-cancel. Pick one
  and be consistent; don't leave sessions hostless.

Useful pairing: when the host is picking, default the map list to `unrated=true` for the
current roster so the group gravitates toward maps they haven't rated yet.

## Discord bot

Consumes backend events and writes to Discord. No commands, no listeners, no
interaction handlers.

Events: `session.created`, `session.ended`, `round.map_selected`, `round.ready_changed`,
`round.started`, `round.ended`.

- On `session.created`: post a root message in `GuildConfig.notification_channel_id`,
  create a thread from it named after the session, and PATCH the thread/message IDs back
  to the backend. Everything else for that session posts inside the thread.
- Keeps **one message per round**, edited in place as the ready count changes. Store
  `SessionRound.message_id` so edits survive a bot restart.
- On `session.ended`: edit the root message with a summary (maps played, duration) and
  archive the thread.
- Required bot permissions: Send Messages, Create Public Threads, Send Messages in
  Threads, Embed Links, Manage Threads (for archiving). Also enable the **Server Members
  privileged intent** in the Discord developer portal — the backend's member lookups
  need it.
- Handle the thread already being archived or deleted — unarchive or fall back to the
  parent channel rather than crashing.

## API surface

```
POST   /maps                      # add from workshop id/url; backend fetches Steam
GET    /maps                      # ?author= &sort=subscribers|rating|created_at|times_played
                                  #  &order= &unrated=true &page=
                                  # unrated filters to maps the *requesting user* has no
                                  # Rating row for. Requires an authenticated caller.
                                  # There is no global played/unplayed flag.
GET    /maps/{id}
POST   /maps/{id}/ratings         # body may include round_id

GET    /guilds                    # guilds with a GuildConfig where the caller is a
                                  # member — resolved by calling membership.is_member
                                  # for each configured guild. Fine at this scale;
                                  # denormalize a membership table if it ever isn't.
PUT    /guilds/{guild_id}/config  # admin only: set notification_channel_id

GET    /sessions                  # ?guild_id= &status=active — list concurrent sessions
GET    /sessions/mine             # the caller's current active session, if any
POST   /sessions                  # body: name, guild_id
GET    /sessions/{id}             # roster + current round + round history
PATCH  /sessions/{id}             # internal: bot reports thread_id/root_message_id
POST   /sessions/{id}/players     # join (409 if already in another session)
DELETE /sessions/{id}/players/me  # leave
POST   /sessions/{id}/complete
POST   /sessions/{id}/cancel      # host/admin: end with status=cancelled

POST   /sessions/{id}/rounds      # host only: pick a map, opens a new round
GET    /sessions/{id}/rounds      # history
PATCH  /rounds/{id}               # host only: status transitions, or swap the map
POST   /rounds/{id}/ready         # caller marks themselves ready

GET    /maps/lookup               # ?workshop_ids=a,b,c (max 200): which are in the library
GET    /guilds/{guild_id}/channels  # admin only: text channels, for the setup page
GET    /tokens, POST /tokens, DELETE /tokens/{id}   # extension API tokens (cookie login only)

# Bot only (X-Internal-Token). Single bot instance assumed.
GET    /internal/events           # undelivered outbox events, oldest first
POST   /internal/events/ack       # body: {ids: [...]} — the events actually processed
GET    /internal/sessions/{id}    # state recovery after a bot restart
PATCH  /internal/rounds/{id}      # bot stores the map-card message_id
```

## Sync

**Skip WebSockets for the MVP.** The session page polls `GET /sessions/{id}` every few
seconds — enough for roster and ready-count updates. Revisit if the polling feels laggy
during ready checks.

## Build order

1. Backend core: models, migrations, `POST /maps` + Steam fetch, `GET /maps`
2. SvelteKit frontend: Discord OAuth2, map grid, add-map form, rating
3. Sessions and rounds: full lifecycle in the UI
4. Discord bot: event consumer, thread creation, session and round embeds
5. API tokens (issued from web settings page)
6. Chrome extension

## Chrome extension (phase 6)

Manifest V3.

- **Content script** on `steamcommunity.com/sharedfiles/filedetails/*` and the workshop
  browse/collection pages. Injects an "Add to Hub" button next to Subscribe, and a
  small per-tile button on grids.
- Extracts only the file ID (from the URL or the tile's `href`) and hands it to the
  backend. No metadata scraping.
- **Service worker** does the actual `fetch` to the API — avoids CORS issues from the
  content script — and reads the token from `chrome.storage.sync`.
- **Popup** for pasting the API token and showing recently added maps.
- On page load, batch-query the API for which visible IDs are already in the library
  and mark those tiles so users don't try to re-add.

## Deferred

Not in the MVP. Listed so the schema stays compatible, not as a to-do list.

- **Group voting per round.** Would add a `voting` status before `downloading` on
  `SessionRound`, a `Vote` table (unique on round_id + discord_user_id, upsert to
  change your pick), a backend-generated ballot of N candidates weighted by
  `last_played_at`, and least-recently-played as the tie-break. `map_id` on
  `SessionRound` becomes nullable until resolution. This is also when WebSockets start
  to earn their keep. The round-based model already accommodates this — voting slots in
  as a phase of a round, so no restructuring is needed.