from app.services.steam import parse_workshop_id
from tests.conftest import ALICE, HOST, add_map


def test_parse_workshop_id():
    assert parse_workshop_id("123456") == "123456"
    assert parse_workshop_id("https://steamcommunity.com/sharedfiles/filedetails/?id=123456&searchtext=x") == "123456"
    assert parse_workshop_id("steamcommunity.com/workshop/filedetails/?id=42") == "42"
    assert parse_workshop_id("https://example.com/?id=42") is None
    assert parse_workshop_id("not a thing") is None
    assert parse_workshop_id("") is None


def test_add_map_requires_login(client):
    assert client.post("/maps", json={"workshop": "111"}).status_code == 401


def test_add_map_by_url_then_idempotent(users, steam):
    host = users["host"]
    resp = host.post("/maps", json={"workshop": "https://steamcommunity.com/sharedfiles/filedetails/?id=111"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["workshop_id"] == "111"
    assert body["title"] == "Alpha Map"
    assert body["author_name"] == "Ann"
    assert body["subscribers"] == 500
    assert body["added_by_discord_id"] == HOST
    assert body["times_played"] == 0
    assert body["workshop_url"].endswith("?id=111")

    # Re-adding returns the existing map with 200 and doesn't hit Steam again.
    again = host.post("/maps", json={"workshop": "111"})
    assert again.status_code == 200
    assert again.json()["id"] == body["id"]
    assert steam.calls == ["111"]


def test_add_map_errors(users):
    host = users["host"]
    assert host.post("/maps", json={"workshop": "garbage"}).json()["detail"]["code"] == "invalid_workshop_ref"
    r = host.post("/maps", json={"workshop": "123123"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "workshop_item_not_found"
    r = host.post("/maps", json={"workshop": "999"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "wrong_app"


def test_lookup(users):
    host = users["host"]
    m = add_map(host, "111")
    r = host.get("/maps/lookup", params={"workshop_ids": "111,222, 555"})
    assert r.status_code == 200
    assert r.json()["found"] == {"111": m["id"]}


def test_list_sort_filter_and_ratings(users):
    host, alice = users["host"], users["alice"]
    a = add_map(host, "111")
    b = add_map(host, "222")
    c = add_map(host, "333")

    # Anonymous read is allowed.
    r = host.client.get("/maps", params={"sort": "subscribers", "order": "desc"})
    assert r.status_code == 200
    assert [m["title"] for m in r.json()["items"]] == ["Gamma Map", "Alpha Map", "Beta Map"]
    assert r.json()["total"] == 3

    r = host.get("/maps", params={"author": "ann"})
    assert {m["title"] for m in r.json()["items"]} == {"Alpha Map", "Gamma Map"}

    r = host.get("/maps", params={"q": "beta"})
    assert [m["title"] for m in r.json()["items"]] == ["Beta Map"]

    # Ratings: one per user per map, upsert on re-rate.
    assert host.post(f"/maps/{a['id']}/ratings", json={"stars": 5}).status_code == 201
    assert host.post(f"/maps/{a['id']}/ratings", json={"stars": 3}).status_code == 201
    assert alice.post(f"/maps/{a['id']}/ratings", json={"stars": 4}).status_code == 201
    assert alice.post(f"/maps/{b['id']}/ratings", json={"stars": 1}).status_code == 201
    assert host.post(f"/maps/{a['id']}/ratings", json={"stars": 6}).status_code == 422

    detail = host.get(f"/maps/{a['id']}").json()
    assert detail["avg_rating"] == 3.5
    assert detail["rating_count"] == 2
    assert detail["my_rating"] == 3
    assert alice.get(f"/maps/{a['id']}").json()["my_rating"] == 4

    r = host.get("/maps", params={"sort": "rating", "order": "desc"})
    titles = [m["title"] for m in r.json()["items"]]
    assert titles[0] == "Alpha Map" and titles[1] == "Beta Map"  # unrated last

    # unrated is per requesting user.
    assert host.client.get("/maps", params={"unrated": "true"}).status_code == 401
    r = host.get("/maps", params={"unrated": "true"})
    assert {m["id"] for m in r.json()["items"]} == {b["id"], c["id"]}
    r = alice.get("/maps", params={"unrated": "true"})
    assert {m["id"] for m in r.json()["items"]} == {c["id"]}

    # pagination
    r = host.get("/maps", params={"page_size": 2, "page": 2, "sort": "title", "order": "asc"})
    assert r.json()["total"] == 3
    assert [m["title"] for m in r.json()["items"]] == ["Gamma Map"]


def test_rating_with_round_id_validates(users, guild):
    host, alice = users["host"], users["alice"]
    a = add_map(host, "111")
    b = add_map(host, "222")
    s = host.post("/sessions", json={"name": "Night", "guild_id": guild}).json()
    rnd = host.post(f"/sessions/{s['id']}/rounds", json={"map_id": a["id"]}).json()

    # wrong map for that round
    r = host.post(f"/maps/{b['id']}/ratings", json={"stars": 4, "round_id": rnd["id"]})
    assert r.json()["detail"]["code"] == "round_map_mismatch"
    # alice wasn't in the round
    r = alice.post(f"/maps/{a['id']}/ratings", json={"stars": 4, "round_id": rnd["id"]})
    assert r.json()["detail"]["code"] == "not_in_round"
    # host was
    r = host.post(f"/maps/{a['id']}/ratings", json={"stars": 4, "round_id": rnd["id"]})
    assert r.status_code == 201 and r.json()["round_id"] == rnd["id"]
    assert r.json()["discord_user_id"] == HOST
    assert ALICE != HOST
