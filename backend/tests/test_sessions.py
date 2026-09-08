import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Map, PlaySession, SessionPlayer, SessionRound, utcnow
from tests.conftest import ALICE, BOB, CHANNEL, GUILD, HOST, add_map


def test_database_enforces_one_active_session_and_one_open_round(db, guild):
    """The partial unique indexes, exercised directly rather than through the API."""
    s1 = PlaySession(name="one", guild_id=guild, host_discord_id=HOST)
    s2 = PlaySession(name="two", guild_id=guild, host_discord_id=ALICE)
    m = Map(workshop_id="1", title="m", added_by_discord_id=HOST)
    db.add_all([s1, s2, m])
    db.commit()
    s1_id, s2_id, map_id = s1.id, s2.id, m.id

    db.add(SessionPlayer(session_id=s1_id, discord_user_id=HOST))
    db.commit()
    db.add(SessionPlayer(session_id=s2_id, discord_user_id=HOST))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # after leaving (left_at set) the same user may be in another session
    row = db.scalar(select(SessionPlayer).where(SessionPlayer.session_id == s1_id))
    row.left_at = utcnow()
    db.add(SessionPlayer(session_id=s2_id, discord_user_id=HOST))
    db.commit()

    db.add(SessionRound(session_id=s1_id, map_id=map_id, round_number=1, status="downloading"))
    db.commit()
    db.add(SessionRound(session_id=s1_id, map_id=map_id, round_number=2, status="playing"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # closed rounds don't count toward the one-open-round limit
    db.add(SessionRound(session_id=s2_id, map_id=map_id, round_number=1, status="completed"))
    db.add(SessionRound(session_id=s2_id, map_id=map_id, round_number=2, status="skipped"))
    db.add(SessionRound(session_id=s2_id, map_id=map_id, round_number=3, status="downloading"))
    db.commit()


def _types(events):
    return [e["type"] for e in events]


def test_create_requires_guild_config_and_membership(users, membership):
    host, outsider = users["host"], users["outsider"]
    r = host.post("/sessions", json={"name": "x", "guild_id": GUILD})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "guild_not_configured"

    membership.add("555", HOST)
    r = outsider.post("/sessions", json={"name": "x", "guild_id": GUILD})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_member"


def test_full_lifecycle(users, guild, bot, db):
    host, alice, bob = users["host"], users["alice"], users["bob"]
    a = add_map(host, "111")
    b = add_map(host, "222")

    # 1. create
    r = host.post("/sessions", json={"name": "Friday", "guild_id": guild})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["status"] == "active"
    assert s["host_discord_id"] == HOST
    assert [p["discord_user_id"] for p in s["players"]] == [HOST]
    assert s["players"][0]["is_host"] and s["players"][0]["user"]["display_name"] == "Host"
    assert s["current_round"] is None

    ev = bot.events()
    assert _types(ev) == ["session.created"]
    assert ev[0]["payload"]["session"]["notification_channel_id"] == CHANNEL
    assert ev[0]["payload"]["session"]["name"] == "Friday"

    # bot reports the thread
    r = bot.patch(f"/sessions/{s['id']}", json={"thread_id": "777", "root_message_id": "778"})
    assert r.status_code == 200 and r.json()["thread_id"] == "777"
    assert host.patch(f"/sessions/{s['id']}", json={"thread_id": "x"}).status_code == 401

    # 2. join
    r = alice.post(f"/sessions/{s['id']}/players")
    assert r.status_code == 200, r.text
    assert {p["discord_user_id"] for p in r.json()["players"]} == {HOST, ALICE}
    assert alice.post(f"/sessions/{s['id']}/players").status_code == 200  # idempotent

    assert alice.get("/sessions/mine").json()["id"] == s["id"]
    assert bob.get("/sessions/mine").json() is None

    # non-host can't pick maps
    r = alice.post(f"/sessions/{s['id']}/rounds", json={"map_id": a["id"]})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "host_required"

    # 3. host picks a map
    r = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": a["id"]})
    assert r.status_code == 201, r.text
    r1 = r.json()
    assert r1["round_number"] == 1 and r1["status"] == "downloading"
    assert {p["discord_user_id"] for p in r1["players"]} == {HOST, ALICE}
    assert r1["ready_count"] == 0 and r1["player_count"] == 2
    assert r1["map"]["times_played"] == 1 and r1["map"]["last_played_at"] is not None

    ev = bot.events()
    assert _types(ev)[-1] == "round.map_selected"
    assert ev[-1]["payload"]["round"]["map"]["workshop_url"].endswith("?id=111")
    assert ev[-1]["payload"]["session"]["thread_id"] == "777"

    # bot stores the map card message id
    assert bot.patch(f"/internal/rounds/{r1['id']}", json={"message_id": "901"}).json()["message_id"] == "901"

    # late joiner during downloading is added to the round
    r = bob.post(f"/sessions/{s['id']}/players")
    assert r.status_code == 200
    assert {p["discord_user_id"] for p in r.json()["current_round"]["players"]} == {HOST, ALICE, BOB}
    assert _types(bot.events())[-1] == "round.ready_changed"

    # 4. ready up
    r = alice.post(f"/rounds/{r1['id']}/ready")
    assert r.status_code == 200 and r.json()["ready_count"] == 1
    r = alice.post(f"/rounds/{r1['id']}/ready", json={"ready": False})
    assert r.json()["ready_count"] == 0
    assert alice.post(f"/rounds/{r1['id']}/ready").json()["ready_count"] == 1
    ev = bot.events()
    assert _types(ev)[-1] == "round.ready_changed"
    assert ev[-1]["payload"]["round"]["ready_count"] == 1
    assert ev[-1]["payload"]["round"]["message_id"] == "901"

    # 5. start
    assert host.patch(f"/rounds/{r1['id']}", json={"status": "completed"}).json()["detail"]["code"] == "invalid_transition"
    r = host.patch(f"/rounds/{r1['id']}", json={"status": "playing"})
    assert r.status_code == 200 and r.json()["status"] == "playing"
    assert _types(bot.events())[-1] == "round.started"

    # can't ready once playing; late joiner not added to a playing round
    assert bob.post(f"/rounds/{r1['id']}/ready").json()["detail"]["code"] == "round_not_downloading"

    # 6. end round
    r = host.patch(f"/rounds/{r1['id']}", json={"status": "completed"})
    assert r.json()["status"] == "completed" and r.json()["ended_at"] is not None
    assert _types(bot.events())[-1] == "round.ended"
    detail = host.get(f"/sessions/{s['id']}").json()
    assert detail["current_round"] is None and len(detail["rounds"]) == 1

    # 7. next round auto-closes, roster snapshot includes bob now
    r2 = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": b["id"]}).json()
    assert r2["round_number"] == 2
    assert {p["discord_user_id"] for p in r2["players"]} == {HOST, ALICE, BOB}
    r3 = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": a["id"]}).json()
    assert r3["round_number"] == 3
    rounds = host.get(f"/sessions/{s['id']}/rounds").json()
    assert [(x["round_number"], x["status"]) for x in rounds] == [(1, "completed"), (2, "skipped"), (3, "downloading")]
    # round 2 never started -> skipped, play stats rolled back
    assert host.get(f"/maps/{b['id']}").json()["times_played"] == 0
    assert host.get(f"/maps/{b['id']}").json()["last_played_at"] is None
    assert host.get(f"/maps/{a['id']}").json()["times_played"] == 2

    # end session: open round closed, everyone gets left_at, thread summary event
    r = host.post(f"/sessions/{s['id']}/complete")
    assert r.status_code == 200
    assert r.json()["status"] == "completed" and r.json()["ended_at"] is not None
    assert r.json()["players"] == []
    open_rows = db.scalars(select(SessionPlayer).where(SessionPlayer.left_at.is_(None))).all()
    assert open_rows == []
    ev = bot.events()
    assert _types(ev)[-2:] == ["round.ended", "session.ended"]
    summary = ev[-1]["payload"]["summary"]
    assert summary["rounds_played"] == 1 and summary["cancelled"] is False
    assert [m["round_number"] for m in summary["maps_played"]] == [1]

    # ack: the bot's cursor lives in the backend
    # explicit ids, not a high-water mark: skipping one in the middle keeps it queued
    ids = [e["id"] for e in ev]
    assert bot.post("/internal/events/ack", json={"ids": ids[:1] + ids[2:]}).json()["acked"] == len(ev) - 1
    assert [e["id"] for e in bot.events()] == [ids[1]]
    assert bot.post("/internal/events/ack", json={"ids": [ids[1]]}).json()["acked"] == 1
    assert bot.events() == []

    # everyone is free to start a new session again
    assert alice.get("/sessions/mine").json() is None
    assert alice.post("/sessions", json={"name": "Again", "guild_id": guild}).status_code == 201
    assert _types(bot.events()) == ["session.created"]
    active = host.get("/sessions", params={"guild_id": guild, "status": "active"}).json()
    assert [x["name"] for x in active] == ["Again"]


def test_one_active_session_per_user(users, guild):
    host, alice = users["host"], users["alice"]
    s1 = host.post("/sessions", json={"name": "One", "guild_id": guild}).json()
    s2 = alice.post("/sessions", json={"name": "Two", "guild_id": guild}).json()  # concurrent sessions are fine
    assert s1["id"] != s2["id"]

    r = alice.post(f"/sessions/{s1['id']}/players")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "already_in_session"
    assert detail["session_id"] == s2["id"] and detail["session_name"] == "Two"

    r = host.post("/sessions", json={"name": "Three", "guild_id": guild})
    assert r.status_code == 409 and r.json()["detail"]["session_id"] == s1["id"]

    # leave, then joining works
    assert alice.delete(f"/sessions/{s2['id']}/players/me").status_code == 200
    assert alice.post(f"/sessions/{s1['id']}/players").status_code == 200


def test_host_leaving_promotes_or_cancels(users, guild, bot):
    host, alice, bob = users["host"], users["alice"], users["bob"]
    s = host.post("/sessions", json={"name": "H", "guild_id": guild}).json()
    alice.post(f"/sessions/{s['id']}/players")
    bob.post(f"/sessions/{s['id']}/players")

    r = host.delete(f"/sessions/{s['id']}/players/me")
    assert r.status_code == 200
    assert r.json()["host_discord_id"] == ALICE  # longest-tenured
    assert r.json()["status"] == "active"

    # the new host can drive rounds
    m = add_map(alice, "111")
    assert alice.post(f"/sessions/{s['id']}/rounds", json={"map_id": m["id"]}).status_code == 201
    assert bob.post(f"/sessions/{s['id']}/rounds", json={"map_id": m["id"]}).status_code == 403

    alice.delete(f"/sessions/{s['id']}/players/me")
    r = bob.delete(f"/sessions/{s['id']}/players/me")
    assert r.json()["status"] == "cancelled"
    assert r.json()["ended_at"] is not None
    ev = bot.events()
    assert _types(ev)[-1] == "session.ended" and ev[-1]["payload"]["summary"]["cancelled"] is True
    # the open round was skipped and stats rolled back
    assert r.json()["rounds"][0]["status"] == "skipped"
    assert alice.get(f"/maps/{m['id']}").json()["times_played"] == 0

    # session is over: no more joins
    assert host.post(f"/sessions/{s['id']}/players").json()["detail"]["code"] == "session_not_active"


def test_session_detail_query_count_is_flat(users, guild, engine):
    """GET /sessions/{id} is polled every few seconds: its query count must not
    grow with the number of rounds."""
    from sqlalchemy import event

    host = users["host"]
    maps = [add_map(host, w) for w in ("111", "222", "333")]
    s = host.post("/sessions", json={"name": "Q", "guild_id": guild}).json()

    def count_queries(n_rounds: int) -> int:
        while len(host.get(f"/sessions/{s['id']}").json()["rounds"]) < n_rounds:
            host.post(f"/sessions/{s['id']}/rounds", json={"map_id": maps[n_rounds % 3]["id"]})
        statements = []
        listener = lambda conn, cursor, stmt, params, ctx, many: statements.append(stmt)  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            assert host.get(f"/sessions/{s['id']}").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(statements)

    assert count_queries(1) == count_queries(6)


def test_leaving_during_download_updates_ready_count(users, guild):
    host, alice = users["host"], users["alice"]
    m = add_map(host, "111")
    s = host.post("/sessions", json={"name": "L", "guild_id": guild}).json()
    alice.post(f"/sessions/{s['id']}/players")
    rnd = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": m["id"]}).json()
    assert rnd["player_count"] == 2
    r = alice.delete(f"/sessions/{s['id']}/players/me")
    assert r.json()["current_round"]["player_count"] == 1


def test_swap_map_and_skip(users, guild, db):
    host = users["host"]
    a = add_map(host, "111")
    b = add_map(host, "222")
    s = host.post("/sessions", json={"name": "S", "guild_id": guild}).json()
    rnd = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": a["id"]}).json()
    host.post(f"/rounds/{rnd['id']}/ready")

    r = host.patch(f"/rounds/{rnd['id']}", json={"map_id": b["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["map"]["id"] == b["id"]
    assert r.json()["ready_count"] == 0  # ready flags reset on swap
    assert db.get(Map, a["id"]).times_played == 0
    assert db.get(Map, b["id"]).times_played == 1

    r = host.patch(f"/rounds/{rnd['id']}", json={"status": "skipped"})
    assert r.json()["status"] == "skipped"
    db.expire_all()
    assert db.get(Map, b["id"]).times_played == 0
    assert db.get(Map, b["id"]).last_played_at is None

    # skipping a playing round keeps the play stats
    rnd2 = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": a["id"]}).json()
    r = host.patch(f"/rounds/{rnd2['id']}", json={"status": "playing"})
    assert r.json()["playing_started_at"] is not None
    assert host.patch(f"/rounds/{rnd2['id']}", json={"map_id": b["id"]}).json()["detail"]["code"] == "round_not_downloading"
    host.patch(f"/rounds/{rnd2['id']}", json={"status": "skipped"})
    db.expire_all()
    assert db.get(Map, a["id"]).times_played == 1
    played_at = db.get(Map, a["id"]).last_played_at
    assert played_at is not None

    # a later never-played round is skipped: last_played_at falls back to the
    # played-then-skipped round, consistent with times_played still counting it
    rnd3 = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": a["id"]}).json()
    host.patch(f"/rounds/{rnd3['id']}", json={"status": "skipped"})
    db.expire_all()
    assert db.get(Map, a["id"]).times_played == 1
    assert db.get(Map, a["id"]).last_played_at == played_at


def test_admin_can_drive_session(users, guild, membership):
    host, alice = users["host"], users["alice"]
    membership.add(GUILD, ALICE, admin=True)
    m = add_map(host, "111")
    s = host.post("/sessions", json={"name": "A", "guild_id": guild}).json()
    assert alice.post(f"/sessions/{s['id']}/rounds", json={"map_id": m["id"]}).status_code == 201
    assert alice.post(f"/sessions/{s['id']}/complete").status_code == 200


def test_non_member_and_discord_outage(users, guild, membership):
    host, outsider, alice = users["host"], users["outsider"], users["alice"]
    s = host.post("/sessions", json={"name": "O", "guild_id": guild}).json()

    r = outsider.get(f"/sessions/{s['id']}")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_member"
    assert outsider.post(f"/sessions/{s['id']}/players").status_code == 403

    membership.available = False
    r = alice.post(f"/sessions/{s['id']}/players")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "discord_unavailable"
    membership.available = True
    assert alice.post(f"/sessions/{s['id']}/players").status_code == 200

    # someone removed from the server can't ready up, but can still leave
    m = add_map(host, "111")
    rnd = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": m["id"]}).json()
    membership.remove(GUILD, ALICE)
    assert alice.post(f"/rounds/{rnd['id']}/ready").json()["detail"]["code"] == "not_a_member"
    assert alice.delete(f"/sessions/{s['id']}/players/me").status_code == 200


def test_unrated_by_session_roster(users, guild):
    host, alice = users["host"], users["alice"]
    a = add_map(host, "111")
    b = add_map(host, "222")
    c = add_map(host, "333")
    host.post(f"/maps/{a['id']}/ratings", json={"stars": 5})
    alice.post(f"/maps/{b['id']}/ratings", json={"stars": 2})
    s = host.post("/sessions", json={"name": "U", "guild_id": guild}).json()
    alice.post(f"/sessions/{s['id']}/players")

    r = host.get("/maps", params={"unrated": "true"})
    assert {m["id"] for m in r.json()["items"]} == {b["id"], c["id"]}
    r = host.get("/maps", params={"unrated_by_session": s["id"]})
    assert {m["id"] for m in r.json()["items"]} == {c["id"]}
    assert users["bob"].get("/maps", params={"unrated_by_session": s["id"]}).status_code == 403


def test_guilds_listing_and_config(users, membership, db):
    host, alice = users["host"], users["alice"]
    assert host.get("/guilds").json() == []

    r = alice.put(f"/guilds/{GUILD}/config", json={"notification_channel_id": CHANNEL})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "admin_required"

    membership.owners[GUILD] = HOST
    r = host.put(f"/guilds/{GUILD}/config", json={"notification_channel_id": "nope"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_channel"
    r = host.put(f"/guilds/{GUILD}/config", json={"notification_channel_id": CHANNEL})
    assert r.status_code == 200 and r.json()["notification_channel_id"] == CHANNEL

    assert host.get(f"/guilds/{GUILD}/channels").json() == [{"channel_id": CHANNEL, "name": "zombies"}]

    mine = host.get("/guilds").json()
    assert len(mine) == 1 and mine[0]["is_admin"] is True and mine[0]["name"].startswith("Guild")
    theirs = alice.get("/guilds").json()
    assert theirs[0]["is_admin"] is False
    assert users["outsider"].get("/guilds").json() == []
