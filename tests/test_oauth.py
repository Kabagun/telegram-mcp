from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from mcp.server.auth.provider import TokenError
from starlette.applications import Starlette

from telegram_ping_mcp.oauth import OAuthProvider, UserAccessToken

ISSUER = "https://connector.example.test"
RESOURCE = ISSUER + "/mcp"
CALLBACK = "http://127.0.0.1:1455/auth/callback"
VERIFIER = "A" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)


@pytest.fixture
def setup(tmp_path):
    users = {
        "synthetic": {"user_key": "synthetic", "telegram_user_id": 7001, "active": True},
        "second": {"user_key": "second", "telegram_user_id": 7002, "active": True},
    }
    path = tmp_path / "oauth.sqlite3"
    provider = OAuthProvider(path, issuer_url=ISSUER, resource_url=RESOURCE, get_user=users.get)
    return provider, users, path


@pytest.fixture
async def client(setup):
    app = Starlette(routes=setup[0].create_routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ISSUER, follow_redirects=False
    ) as value:
        yield value


async def register(client, *, method="none", callback=CALLBACK, **metadata):
    response = await client.post(
        "/register",
        json={
            "redirect_uris": [callback],
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
            "client_name": "Synthetic AI client",
            **metadata,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def begin(client, registration, **params):
    response = await client.get(
        "/authorize",
        params={
            "client_id": registration["client_id"],
            "redirect_uri": registration["redirect_uris"][0],
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "synthetic state & value",
            **params,
        },
    )
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith(ISSUER + "/oauth/consent?")
    nonce = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
    page = await client.get(response.headers["location"])
    assert page.status_code == 200, page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return {"request": nonce, "csrf": csrf}, page


async def approve(client, provider, registration, *, user="synthetic"):
    fields, _ = await begin(client, registration)
    login = provider.issue_login_code(user)
    response = await client.post(
        "/oauth/consent",
        data={**fields, "code": login, "decision": "allow"},
        headers={"Origin": ISSUER},
    )
    assert response.status_code == 302, response.text
    callback = parse_qs(urlsplit(response.headers["location"]).query)
    assert callback["state"] == ["synthetic state & value"]
    return callback["code"][0], login, fields


async def exchange(client, registration, code, **overrides):
    return await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "redirect_uri": registration["redirect_uris"][0],
            "code": code,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
            **overrides,
        },
    )


async def token_pair(client, provider, registration, *, user="synthetic"):
    code, _, _ = await approve(client, provider, registration, user=user)
    response = await exchange(client, registration, code)
    assert response.status_code == 200, response.text
    return response.json()


async def test_complete_oauth_persistence_rotation_and_revocation(client, setup):
    provider, users, path = setup
    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["authorization_endpoint"] == ISSUER + "/authorize"
    assert metadata["registration_endpoint"] == ISSUER + "/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    assert metadata["client_id_metadata_document_supported"] is False
    registration = await register(client)
    code, login, _ = await approve(client, provider, registration)
    response = await exchange(client, registration, code)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["scope"] == "telegram"
    assert len(base64.urlsafe_b64decode(tokens["access_token"] + "=")) == 32
    token = await provider.load_access_token(tokens["access_token"])
    assert isinstance(token, UserAccessToken)
    assert token.user_key == "synthetic" and token.telegram_user_id == 7001
    assert token.resource == RESOURCE
    restarted = OAuthProvider(path, issuer_url=ISSUER, resource_url=RESOURCE, get_user=users.get)
    assert await restarted.verify_token(tokens["access_token"]) is not None
    assert (await exchange(client, registration, code)).status_code == 400
    with sqlite3.connect(path) as db:
        dump = "\n".join(db.iterdump())
    for credential in (
        code,
        login,
        login.replace("-", ""),
        *(tokens[name] for name in ("access_token", "refresh_token")),
    ):
        assert credential not in dump
    refresh = await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refresh.status_code == 200, refresh.text
    rotated = refresh.json()
    assert rotated["access_token"] != tokens["access_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert await provider.load_access_token(tokens["access_token"]) is None
    assert await provider.load_access_token(rotated["access_token"]) is not None
    revoked = await client.post(
        "/revoke",
        data={"client_id": registration["client_id"], "token": rotated["access_token"]},
    )
    assert revoked.status_code == 200, revoked.text
    assert await provider.load_access_token(rotated["access_token"]) is None
    registered = await provider.get_client(registration["client_id"])
    assert await provider.load_refresh_token(registered, rotated["refresh_token"]) is None


@pytest.mark.parametrize("explicit", [False, True])
async def test_auth_code_only_registration_without_refresh_grant(client, setup, explicit):
    metadata = {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"}
    if explicit:
        metadata["grant_types"] = ["authorization_code"]
    registration = (await client.post("/register", json=metadata)).json()
    assert registration["grant_types"] == ["authorization_code"]
    tokens = await token_pair(client, setup[0], registration)
    assert tokens.get("refresh_token") is None
    assert await setup[0].load_access_token(tokens["access_token"]) is not None


async def test_unused_registration_expires_while_live_grants_keep_clients(client, setup):
    provider, _, path = setup
    active, unused = await register(client), await register(client)
    tokens = await token_pair(client, provider, active)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE clients SET expires_at = 0")
    assert await provider.get_client(active["client_id"]) is not None
    assert await provider.get_client(unused["client_id"]) is None
    # Any subsequent allocation collects old unused registrations.
    await register(client)
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM clients WHERE client_id = ?", (unused["client_id"],)
            ).fetchone()[0]
            == 0
        )
    assert await provider.load_access_token(tokens["access_token"]) is not None


async def test_csrf_origin_and_cookie_fail_before_code_consumption(client, setup):
    provider = setup[0]
    registration = await register(client)
    fields, page = await begin(client, registration)
    assert "AI Connector" in page.text and "Код подключения из бота" in page.text
    assert "Подтвердите связь с вашим чатом" in page.text
    assert "127.0.0.1:1455" in page.text
    assert "может читать сообщения и файлы, которые вы отправляете этому боту" in page.text
    assert "не входит в Telegram-аккаунт" in page.text
    assert "не видит остальные переписки, контакты и группы" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "Secure" in page.headers["set-cookie"]
    assert "SameSite=lax" in page.headers["set-cookie"]
    login = provider.issue_login_code("synthetic")
    data = {**fields, "code": login, "decision": "allow"}
    assert (await client.post("/oauth/consent", data=data)).status_code == 403
    assert (
        await client.post(
            "/oauth/consent",
            data=data,
            headers={"origin": "https://attacker.example.test"},
        )
    ).status_code == 403
    assert (
        await client.post(
            "/oauth/consent",
            data={**data, "csrf": "bad"},
            headers={"origin": ISSUER},
        )
    ).status_code == 403
    cookie = client.cookies.get("__Secure-ai-connector-csrf")
    client.cookies.clear()
    assert (
        await client.post(
            "/oauth/consent",
            data=data,
            headers={"origin": ISSUER},
        )
    ).status_code == 403
    client.cookies.set(
        "__Secure-ai-connector-csrf", cookie, domain="connector.example.test", path="/oauth/consent"
    )
    assert (
        await client.post(
            "/oauth/consent",
            data=data,
            headers={"origin": ISSUER},
        )
    ).status_code == 302
    assert (
        await client.post(
            "/oauth/consent",
            data=data,
            headers={"origin": ISSUER},
        )
    ).status_code == 403


async def test_pkce_redirect_client_and_resource_isolation(client, setup):
    provider = setup[0]
    registration, other = await register(client), await register(client)
    code, _, _ = await approve(client, provider, registration)
    assert (await exchange(client, other, code)).status_code == 400
    assert (await exchange(client, registration, code, code_verifier="B" * 43)).status_code == 400
    assert (await exchange(client, registration, code, code_verifier="short")).status_code == 400
    assert (
        await exchange(
            client,
            registration,
            code,
            redirect_uri="https://attacker.example.test/callback",
        )
    ).status_code == 400
    assert (
        await exchange(
            client,
            registration,
            code,
            resource="https://attacker.example.test/mcp",
        )
    ).status_code == 400
    assert (await exchange(client, registration, code, resource=RESOURCE + "/")).status_code == 400
    tokens_response = await exchange(client, registration, code)
    assert tokens_response.status_code == 200
    tokens = tokens_response.json()
    refresh_data = {
        "grant_type": "refresh_token",
        "client_id": registration["client_id"],
        "refresh_token": tokens["refresh_token"],
    }
    assert (
        await client.post(
            "/token",
            data={**refresh_data, "resource": "https://attacker.example.test/mcp"},
        )
    ).status_code == 400
    assert (
        await client.post(
            "/token",
            data={**refresh_data, "client_id": other["client_id"]},
        )
    ).status_code == 400
    assert (
        await client.post(
            "/token",
            data={**refresh_data, "scope": "admin"},
        )
    ).status_code == 400
    # Another registered public client cannot revoke these credentials.
    assert (
        await client.post(
            "/revoke",
            data={"client_id": other["client_id"], "token": tokens["access_token"]},
        )
    ).status_code == 200
    assert await provider.load_access_token(tokens["access_token"]) is not None
    assert (await client.post("/token", data=refresh_data)).status_code == 200


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
async def test_confidential_client_hash_only_authentication(client, setup, method):
    provider, _, path = setup
    registration = await register(client, method=method)
    code, _, _ = await approve(client, provider, registration)
    assert (await exchange(client, registration, code)).status_code == 401
    assert (
        await exchange(
            client,
            registration,
            code,
            client_secret="synthetic-wrong-secret",
        )
    ).status_code == 401
    data = {
        "grant_type": "authorization_code",
        "client_id": registration["client_id"],
        "redirect_uri": CALLBACK,
        "code": code,
        "code_verifier": VERIFIER,
    }
    headers = {}
    if method == "client_secret_post":
        data["client_secret"] = registration["client_secret"]
    else:
        credentials = registration["client_id"] + ":" + registration["client_secret"]
        headers["authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
        del data["client_id"]
    response = await client.post("/token", data=data, headers=headers)
    assert response.status_code == 200, response.text
    with sqlite3.connect(path) as db:
        assert registration["client_secret"] not in "\n".join(db.iterdump())
    stored = await provider.get_client(registration["client_id"])
    assert stored.client_secret is None


async def test_identity_is_rechecked_for_access_refresh_and_codes(client, setup):
    provider, users, _ = setup
    registration = await register(client)
    tokens = await token_pair(client, provider, registration)
    code, _, _ = await approve(client, provider, registration)
    registered = await provider.get_client(registration["client_id"])
    for changed in ({"active": False}, {"telegram_user_id": 7999}, {"user_key": "second"}):
        original = users["synthetic"].copy()
        users["synthetic"].update(changed)
        assert await provider.load_access_token(tokens["access_token"]) is None
        assert await provider.load_refresh_token(registered, tokens["refresh_token"]) is None
        assert await provider.load_authorization_code(registered, code) is None
        if changed != {"telegram_user_id": 7999}:
            with pytest.raises(ValueError):
                provider.issue_login_code("synthetic")
        users["synthetic"] = original

    def unavailable(key):
        raise RuntimeError("synthetic unavailable identity store")

    provider.get_user = unavailable
    assert await provider.load_access_token(tokens["access_token"]) is None
    assert await provider.load_refresh_token(registered, tokens["refresh_token"]) is None


async def test_replayed_refresh_revokes_the_rotated_family(client, setup):
    provider = setup[0]
    registration = await register(client)
    tokens = await token_pair(client, provider, registration)
    registered = await provider.get_client(registration["client_id"])
    loaded = await provider.load_refresh_token(registered, tokens["refresh_token"])
    # Simulate two requests loading the same credential before either exchanges it.
    second_loaded = await provider.load_refresh_token(registered, tokens["refresh_token"])
    rotated = await provider.exchange_refresh_token(registered, loaded, loaded.scopes)
    assert await provider.load_access_token(rotated.access_token) is not None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(registered, second_loaded, second_loaded.scopes)
    assert await provider.load_access_token(rotated.access_token) is None
    assert await provider.load_refresh_token(registered, rotated.refresh_token) is None
    # The HTTP path detects reused rotated credentials when loading them as well.
    tokens = await token_pair(client, provider, registration)
    request = {
        "grant_type": "refresh_token",
        "client_id": registration["client_id"],
        "refresh_token": tokens["refresh_token"],
    }
    rotated = (await client.post("/token", data=request)).json()
    assert (await client.post("/token", data=request)).status_code == 400
    assert await provider.load_access_token(rotated["access_token"]) is None


async def test_expiry_and_single_use_are_checked_at_exchange(client, setup):
    provider, _, path = setup
    registration = await register(client)
    registered = await provider.get_client(registration["client_id"])
    code, _, _ = await approve(client, provider, registration)
    first = await provider.load_authorization_code(registered, code)
    second = await provider.load_authorization_code(registered, code)
    tokens = await provider.exchange_authorization_code(registered, first)
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(registered, second)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE grants SET access_expires = 0")
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(registered, tokens.refresh_token) is not None
    with sqlite3.connect(path) as db:
        db.execute("UPDATE grants SET refresh_expires = 0")
    assert await provider.load_refresh_token(registered, tokens.refresh_token) is None
    fields, _ = await begin(client, registration)
    login = provider.issue_login_code("synthetic")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE login_codes SET expires_at = 0")
    assert (
        await client.post(
            "/oauth/consent",
            data={**fields, "code": login, "decision": "allow"},
            headers={"origin": ISSUER},
        )
    ).status_code == 400
    with sqlite3.connect(path) as db:
        db.execute("UPDATE pending SET expires_at = 0")
    assert (
        await client.get("/oauth/consent", params={"request": fields["request"]})
    ).status_code == 400


async def test_pending_login_and_grant_capacity_are_bounded(client, setup, monkeypatch):
    provider = setup[0]
    registration = await register(client)
    fields, _ = await begin(client, registration)
    monkeypatch.setattr("telegram_ping_mcp.oauth.MAX_PENDING", 1)
    response = await client.get(
        "/authorize",
        params={
            "client_id": registration["client_id"],
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "resource": RESOURCE,
        },
    )
    assert "temporarily_unavailable" in response.headers["location"]
    monkeypatch.setattr("telegram_ping_mcp.oauth.MAX_LOGIN_CODES", 1)
    login = provider.issue_login_code("synthetic")
    with pytest.raises(ValueError):
        provider.issue_login_code("second")
    # A replacement for the same user does not need another storage slot.
    login = provider.issue_login_code("synthetic")
    consent = await client.post(
        "/oauth/consent",
        data={**fields, "code": login, "decision": "allow"},
        headers={"origin": ISSUER},
    )
    code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
    monkeypatch.setattr("telegram_ping_mcp.oauth.MAX_GRANTS", 0)
    assert (await exchange(client, registration, code)).status_code == 400


async def test_revoke_user_prevents_rebinding_from_resurrecting_credentials(client, setup):
    provider, users, _ = setup
    registration = await register(client)
    tokens = await token_pair(client, provider, registration)
    other_tokens = await token_pair(client, provider, registration, user="second")
    code, _, _ = await approve(client, provider, registration)
    login = provider.issue_login_code("synthetic")
    registered = await provider.get_client(registration["client_id"])
    provider.revoke_user("synthetic")
    users["synthetic"]["active"] = False
    users["synthetic"]["active"] = True
    assert await provider.load_access_token(tokens["access_token"]) is None
    assert await provider.load_refresh_token(registered, tokens["refresh_token"]) is None
    assert await provider.load_authorization_code(registered, code) is None
    assert await provider.load_access_token(other_tokens["access_token"]) is not None
    fields, _ = await begin(client, registration)
    assert (
        await client.post(
            "/oauth/consent",
            data={**fields, "code": login, "decision": "allow"},
            headers={"origin": ISSUER},
        )
    ).status_code == 400


async def test_login_latest_supersedes_previous_retry_limits_and_denial(client, setup):
    provider = setup[0]
    registration = await register(client)
    fields, _ = await begin(client, registration)
    old = provider.issue_login_code("synthetic")
    current = provider.issue_login_code("synthetic")
    for _ in range(5):
        assert (
            await client.post(
                "/oauth/consent",
                data={**fields, "code": old, "decision": "allow"},
                headers={"origin": ISSUER},
            )
        ).status_code == 400
    assert (
        await client.post(
            "/oauth/consent",
            data={**fields, "code": current, "decision": "allow"},
            headers={"origin": ISSUER},
        )
    ).status_code == 403
    fields, _ = await begin(client, registration)
    denied = await client.post(
        "/oauth/consent",
        data={**fields, "decision": "deny"},
        headers={"origin": ISSUER},
    )
    assert denied.status_code == 302
    callback = parse_qs(urlsplit(denied.headers["location"]).query)
    assert callback["error"] == ["access_denied"]
    assert callback["state"] == ["synthetic state & value"]
    # Denying did not consume the current one-time Telegram code.
    fields, _ = await begin(client, registration)
    assert (
        await client.post(
            "/oauth/consent",
            data={**fields, "code": current.lower(), "decision": "allow"},
            headers={"origin": ISSUER},
        )
    ).status_code == 302


@pytest.mark.parametrize(
    "callback",
    [
        "javascript:alert(1)",
        "https://user:pass@example.test/callback",
        "https://example.test/callback#fragment",
        "http://localhost/callback",
        "http://127.0.0.1.attacker.test/callback",
        "http://example.test/callback",
        "file:///tmp/callback",
    ],
)
async def test_registration_rejects_unsafe_redirects(client, callback):
    response = await client.post(
        "/register",
        json={"redirect_uris": [callback], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


async def test_registration_bounds_cimd_and_callback_errors(client, setup):
    for metadata in (
        {"client_name": "x" * 201},
        {"redirect_uris": [CALLBACK] * 11},
        {"jwks_uri": "https://internal.example.test/keys"},
        {"token_endpoint_auth_method": "private_key_jwt"},
    ):
        response = await client.post(
            "/register",
            json={"redirect_uris": [CALLBACK], **metadata},
        )
        assert response.status_code == 400
    registration = await register(client, callback="https://ai.example.test/oauth/callback")
    params = {
        "client_id": registration["client_id"],
        "redirect_uri": registration["redirect_uris"][0],
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "state": "synthetic",
        "resource": RESOURCE,
    }
    for change in (
        {"resource": "https://attacker.example.test/mcp"},
        {"resource": ""},
        {"code_challenge_method": "plain"},
        {"code_challenge": ""},
    ):
        response = await client.get("/authorize", params={**params, **change})
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["error"] == ["invalid_request"]
        assert query["state"] == ["synthetic"]
    response = await client.get(
        "/authorize",
        params={**params, "redirect_uri": "https://attacker.example.test/callback"},
    )
    assert response.status_code == 400
    # URL-shaped client identifiers are not fetched as metadata documents.
    response = await client.get(
        "/authorize",
        params={**params, "client_id": "http://127.0.0.1/internal"},
    )
    assert response.status_code == 400


async def test_body_duplicate_limits_expiry_and_bounded_registration(client, setup, monkeypatch):
    provider, _, path = setup
    assert (
        await client.post(
            "/register",
            content=b"x" * 16385,
            headers={"content-type": "application/json"},
        )
    ).status_code == 413
    assert (
        await client.post(
            "/register",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
    ).status_code == 400
    registration = await register(client)
    code, _, _ = await approve(client, provider, registration)
    encoded = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "redirect_uri": CALLBACK,
            "code": code,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
        }
    )
    assert (
        await client.post(
            "/token",
            content=encoded + "&resource=https://attacker.example.test/mcp",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    ).status_code == 400
    with sqlite3.connect(path) as db:
        db.execute("UPDATE codes SET expires_at = 0")
    assert (await exchange(client, registration, code)).status_code == 400
    monkeypatch.setattr("telegram_ping_mcp.oauth.MAX_CLIENTS", 1)
    response = await client.post(
        "/register",
        json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 400
    with sqlite3.connect(path) as db:
        db.execute("UPDATE rate_limits SET count = 30 WHERE category = '/register'")
    assert (await client.post("/register", json={"redirect_uris": [CALLBACK]})).status_code == 429


@pytest.mark.parametrize(
    "issuer, resource",
    [
        ("http://example.test", "http://example.test/mcp"),
        (ISSUER + "/nested", RESOURCE),
        (ISSUER + "?query=1", RESOURCE),
        (ISSUER, "https://attacker.example.test/mcp"),
    ],
)
def test_constructor_rejects_inconsistent_issuer_resource(tmp_path: Path, issuer, resource):
    with pytest.raises(ValueError):
        OAuthProvider(
            tmp_path / "oauth.sqlite3",
            issuer_url=issuer,
            resource_url=resource,
            get_user=lambda key: None,
        )
