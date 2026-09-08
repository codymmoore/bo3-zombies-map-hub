from urllib.parse import parse_qs, urlparse

from app.auth.discord_oauth import DiscordIdentity
from app.config import get_settings
from tests.conftest import HOST


def test_me_requires_auth(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", cookies={get_settings().session_cookie_name: "junk"}).status_code == 401


def test_me_via_cookie(users):
    r = users["host"].get("/auth/me")
    assert r.status_code == 200
    assert r.json()["discord_id"] == HOST
    assert r.json()["auth_via"] == "cookie"


def test_login_redirect_and_callback(client, db, monkeypatch):
    r = client.get("/auth/discord/login", params={"next": "/maps"}, follow_redirects=False)
    assert r.status_code == 302
    target = urlparse(r.headers["location"])
    assert target.netloc == "discord.com"
    q = parse_qs(target.query)
    assert q["scope"] == ["identify"]
    state = q["state"][0]
    nonce_cookie = r.cookies.get("oauth_nonce")
    assert nonce_cookie

    from app import deps

    class FakeOAuth:
        def exchange_code(self, code):
            assert code == "abc"
            return DiscordIdentity(discord_id="42", username="zed", global_name="Zed", avatar=None)

    client.app.dependency_overrides[deps.get_oauth] = lambda: FakeOAuth()

    # wrong nonce cookie -> rejected
    bad = client.get("/auth/discord/callback", params={"code": "abc", "state": state},
                     cookies={"oauth_nonce": "other"}, follow_redirects=False)
    assert bad.status_code == 400

    r = client.get("/auth/discord/callback", params={"code": "abc", "state": state},
                   cookies={"oauth_nonce": nonce_cookie}, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == f"{get_settings().frontend_url}/maps"
    session_cookie = r.cookies.get(get_settings().session_cookie_name)
    assert session_cookie

    me = client.get("/auth/me", cookies={get_settings().session_cookie_name: session_cookie}).json()
    assert me["discord_id"] == "42" and me["display_name"] == "Zed"

    # logout clears the cookie
    r = client.post("/auth/logout", cookies={get_settings().session_cookie_name: session_cookie})
    assert r.status_code == 204
    assert "hub_session=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]


def test_api_tokens(users, client):
    host = users["host"]
    r = host.post("/tokens", json={"name": "extension"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert token.startswith("hub_")
    token_id = r.json()["id"]

    listed = host.get("/tokens").json()
    assert [t["name"] for t in listed] == ["extension"]
    assert "token" not in listed[0] and listed[0]["token_prefix"] == token[:12]

    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["auth_via"] == "token"

    # tokens can't manage tokens
    assert client.post("/tokens", json={"name": "x"}, headers=headers).status_code == 403
    assert client.get("/tokens", headers=headers).status_code == 403

    # tokens can add maps (the extension's whole job)
    assert client.post("/maps", json={"workshop": "111"}, headers=headers).status_code == 201

    assert host.delete(f"/tokens/{token_id}").status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert host.get("/tokens").json() == []
    assert client.get("/auth/me", headers={"Authorization": "Bearer hub_bogus"}).status_code == 401
