"""Local OAuth 2.1 authorization server for verified Telegram bot users.

Only hashes of bearer credentials and one-time codes are persisted. The SDK owns
OAuth parsing and protocol responses; the guards below add bounded input,
resource binding and a local, explicit resource-owner consent ceremony.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyHttpUrl, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from .onboarding import PRIVACY_NOTICE

OAUTH_SCOPE = "telegram"
SCOPE = OAUTH_SCOPE
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
LOGIN_TTL = 600
CLIENT_TTL = 86400
MAX_CLIENTS = 1000
MAX_PENDING = 1000
MAX_GRANTS = 10000
MAX_REFRESH_HISTORY = 100000
MAX_LOGIN_CODES = 1000
MAX_PENDING_ATTEMPTS = 5
_COOKIE = "__Secure-ai-connector-csrf"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}
_CONSENT_STYLE = (
    "<style>:root{font-family:system-ui,sans-serif;color:#163454;background:#eef6ff}"
    "*{box-sizing:border-box}body{margin:0;padding:32px 16px}"
    "main{max-width:480px;margin:6vh auto;background:#fff;padding:28px;"
    "border:1px solid #d5e7fa;border-radius:20px;box-shadow:0 12px 40px #245a8d12}"
    "h1{color:#185ca6;font-size:26px;margin:0 0 12px}h2{font-size:21px;line-height:1.35}"
    "p{line-height:1.6;overflow-wrap:anywhere}label{display:block;font-weight:600;margin-top:24px}"
    "input,button{font:inherit;width:100%;border-radius:10px;padding:13px;margin-top:10px}"
    "input{border:1px solid #91b5dc;background:#f8fbff;color:#163454}"
    "button{border:1px solid #246ac0;background:#246ac0;color:white;cursor:pointer;font-weight:600}"
    "button.secondary{background:#eef6ff;color:#185ca6;border-color:#c7ddf4}"
    "input:focus,button:focus-visible{outline:3px solid #91c6ff;outline-offset:2px}"
    "@media(max-width:480px){body{padding:16px}main{margin:16px auto;padding:24px}}"
    "</style>"
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _opaque() -> str:
    return secrets.token_urlsafe(32)


def _safe_redirect(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "#" in value
            or re.fullmatch(r"[A-Za-z0-9._:\[\]-]+", parsed.netloc) is None
            or len(value) > 2048
            or any(ord(char) < 33 for char in value)
        ):
            return False
        if parsed.scheme == "https":
            return True
        return parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class UserAccessToken(AccessToken):
    user_key: str
    telegram_user_id: int


class _UserAuthorizationCode(AuthorizationCode):
    user_key: str
    telegram_user_id: int


class _UserRefreshToken(RefreshToken):
    user_key: str
    telegram_user_id: int


class _Database:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, kind, value, traceback) -> None:
        try:
            self.connection.__exit__(kind, value, traceback)
        finally:
            self.connection.close()


class _HashedClientAuthenticator(ClientAuthenticator):
    """Use the SDK handlers while keeping confidential-client secrets hashed."""

    def __init__(self, provider: OAuthProvider):
        super().__init__(provider)
        self.oauth_provider = provider

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        form = await request.form()
        client_id = str(form.get("client_id", ""))
        client = await self.oauth_provider.get_client(client_id)
        if client is None:
            raise AuthenticationError("Invalid client credentials")
        method = client.token_endpoint_auth_method
        provided = None
        if method == "client_secret_basic":
            auth = request.headers.get("authorization", "")
            try:
                if not auth.startswith("Basic "):
                    raise ValueError
                basic_id, provided = (
                    base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
                )
                if unquote(basic_id) != client_id:
                    raise ValueError
                provided = unquote(provided)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise AuthenticationError("Invalid client credentials") from None
        elif method == "client_secret_post":
            provided = form.get("client_secret")
        elif method != "none":
            raise AuthenticationError("Invalid client credentials")
        with self.oauth_provider._db() as db:
            row = db.execute(
                "SELECT secret_hash FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if method != "none" and (
            row is None
            or not isinstance(provided, str)
            or not row["secret_hash"]
            or not hmac.compare_digest(_hash(provided), row["secret_hash"])
            or (
                client.client_secret_expires_at is not None
                and client.client_secret_expires_at <= time.time()
            )
        ):
            raise AuthenticationError("Invalid client credentials")
        return client


class _RequestGuard:
    """Bound even chunked requests before the SDK parses attacker-controlled data."""

    def __init__(self, app: ASGIApp, provider: OAuthProvider, path: str):
        self.app, self.provider, self.path = app, provider, path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        limit = 16384 if self.path == "/register" else 8192
        body = bytearray()
        if len(scope.get("query_string", b"")) > 8192:
            await self._error(scope, receive, send, 414, "invalid_request")
            return
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await self._error(scope, receive, send, 413, "invalid_request")
                return
            if not message.get("more_body", False):
                break
        rates = {"/register": (30, 3600), "/authorize": (120, 60), "/oauth/consent": (120, 60)}
        maximum, window = rates.get(self.path, (300, 60))
        if not self.provider._rate(self.path, maximum, window):
            await self._error(scope, receive, send, 429, "temporarily_unavailable")
            return
        if self.path != "/register":
            try:
                encoded = (
                    scope.get("query_string", b"") if scope["method"] == "GET" else bytes(body)
                )
                if scope["method"] == "POST":
                    headers = dict(scope.get("headers", []))
                    content_type = headers.get(b"content-type", b"").split(b";", 1)[0]
                    if content_type != b"application/x-www-form-urlencoded":
                        raise ValueError
                params = parse_qs(encoded.decode(), keep_blank_values=True, max_num_fields=30)
                if any(len(values) != 1 for values in params.values()):
                    raise ValueError
                if self.path in ("/token", "/revoke") and "client_id" not in params:
                    auth = dict(scope.get("headers", [])).get(b"authorization", b"")
                    if auth.startswith(b"Basic "):
                        try:
                            basic_id, _ = (
                                base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
                            )
                            client_id = unquote(basic_id)
                        except (ValueError, UnicodeDecodeError, binascii.Error):
                            raise ValueError from None
                        body.extend(("&" + urlencode({"client_id": client_id})).encode())
                if self.path == "/token":
                    resource = params.get("resource")
                    if resource is not None and resource != [self.provider.resource_url]:
                        await self._error(scope, receive, send, 400, "invalid_request")
                        return
                    if params.get("grant_type") == ["authorization_code"]:
                        verifier = params.get("code_verifier", [""])[0]
                        if re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) is None:
                            raise ValueError
                # The SDK requires a present client_secret key even for public revocation.
                if self.path == "/revoke" and "client_secret" not in params:
                    body.extend(b"&client_secret=")
                if len(body) != len(encoded):
                    scope = dict(scope)
                    scope["headers"] = [
                        (key, value) for key, value in scope["headers"] if key != b"content-length"
                    ]
            except (ValueError, UnicodeDecodeError):
                await self._error(scope, receive, send, 400, "invalid_request")
                return
        elif body:
            try:
                if not isinstance(json.loads(body), dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError, RecursionError):
                await self._error(scope, receive, send, 400, "invalid_client_metadata")
                return
        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _error(scope, receive, send, status: int, error: str) -> None:
        await JSONResponse({"error": error}, status, headers=_SECURITY_HEADERS)(
            scope, receive, send
        )


class OAuthProvider(
    OAuthAuthorizationServerProvider[_UserAuthorizationCode, _UserRefreshToken, UserAccessToken]
):
    def __init__(
        self,
        database_path: Path,
        *,
        issuer_url: str,
        resource_url: str,
        get_user: Callable[[str], dict | None],
    ) -> None:
        issuer = urlsplit(issuer_url)
        resource = urlsplit(resource_url)
        if (
            not _safe_redirect(issuer_url)
            or issuer.scheme != "https"
            or issuer.path not in ("", "/")
            or issuer.query
            or not _safe_redirect(resource_url)
            or resource.scheme != "https"
            or resource.netloc != issuer.netloc
            or resource.query
        ):
            raise ValueError("OAuth requires a root HTTPS issuer and a same-origin resource URL")
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.issuer_url = str(AnyHttpUrl(issuer_url)).rstrip("/")
        self.resource_url = resource_url
        self.get_user = get_user
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                    secret_hash TEXT, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending (
                    nonce_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    params TEXT NOT NULL, csrf_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_codes (
                    user_key TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
                    telegram_user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS codes (
                    code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    data TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grants (
                    refresh_hash TEXT PRIMARY KEY, access_hash TEXT UNIQUE NOT NULL,
                    client_id TEXT NOT NULL, user_key TEXT NOT NULL,
                    telegram_user_id INTEGER NOT NULL, scopes TEXT NOT NULL,
                    resource TEXT NOT NULL, access_expires INTEGER NOT NULL,
                    refresh_expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    category TEXT PRIMARY KEY, start INTEGER NOT NULL, count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_history (
                    refresh_hash TEXT PRIMARY KEY, family_hash TEXT NOT NULL,
                    client_id TEXT NOT NULL, user_key TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
            """)
            if "family_hash" not in {
                row["name"] for row in db.execute("PRAGMA table_info(grants)")
            }:
                db.execute("ALTER TABLE grants ADD COLUMN family_hash TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE grants SET family_hash = refresh_hash")
            self._gc(db)

    def _db(self):
        return _Database(self.database_path)

    @staticmethod
    def _gc(db: sqlite3.Connection) -> None:
        now = int(time.time())
        db.execute(
            "DELETE FROM clients WHERE expires_at <= ? AND client_id NOT IN "
            "(SELECT client_id FROM grants WHERE refresh_expires > ?)",
            (now, now),
        )
        for table in ("pending", "login_codes", "codes", "refresh_history"):
            db.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (now,))
        db.execute("DELETE FROM grants WHERE refresh_expires <= ?", (now,))
        db.execute("DELETE FROM pending WHERE client_id NOT IN (SELECT client_id FROM clients)")
        db.execute("DELETE FROM codes WHERE client_id NOT IN (SELECT client_id FROM clients)")
        db.execute("DELETE FROM grants WHERE client_id NOT IN (SELECT client_id FROM clients)")
        db.execute(
            "DELETE FROM refresh_history WHERE client_id NOT IN (SELECT client_id FROM clients)"
        )

    def _rate(self, category: str, maximum: int, seconds: int) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            row = db.execute("SELECT * FROM rate_limits WHERE category = ?", (category,)).fetchone()
            if row is None or row["start"] + seconds <= now:
                db.execute("INSERT OR REPLACE INTO rate_limits VALUES (?, ?, 1)", (category, now))
                return True
            if row["count"] >= maximum:
                return False
            db.execute("UPDATE rate_limits SET count = count + 1 WHERE category = ?", (category,))
            return True

    def _user(self, user_key: str, telegram_id: int | None = None) -> dict | None:
        try:
            user = self.get_user(user_key)
            if (
                user is None
                or user.get("active") is not True
                or user.get("user_key") != user_key
                or type(user.get("telegram_user_id")) is not int
                or user["telegram_user_id"] <= 0
                or ("chat_id" in user and user["chat_id"] != user["telegram_user_id"])
                or (telegram_id is not None and user["telegram_user_id"] != telegram_id)
            ):
                return None
            return user
        except Exception:
            # The identity store may be unavailable during startup or shutdown.
            return None

    def issue_login_code(self, user_key: str) -> str:
        user = self._user(user_key)
        if user is None:
            raise ValueError("An active verified Telegram user is required")
        if not self._rate("login-code", 120, 60):
            raise ValueError("Too many connection attempts; try again later")
        # 160 random bits, grouped for copying from Telegram.
        raw = base64.b32encode(secrets.token_bytes(20)).decode()
        code = "-".join(raw[index : index + 4] for index in range(0, len(raw), 4))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            existing = db.execute("SELECT 1 FROM login_codes WHERE user_key = ?", (user_key,))
            if (
                existing.fetchone() is None
                and db.execute("SELECT COUNT(*) FROM login_codes").fetchone()[0] >= MAX_LOGIN_CODES
            ):
                raise ValueError("Connection capacity reached; try again later")
            db.execute(
                "INSERT OR REPLACE INTO login_codes VALUES (?, ?, ?, ?)",
                (
                    user_key,
                    _hash(self._normalize_login(code)),
                    user["telegram_user_id"],
                    int(time.time()) + LOGIN_TTL,
                ),
            )
        return code

    def revoke_user(self, user_key: str) -> None:
        """Permanently remove credentials before an account binding is unlinked."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_codes WHERE user_key = ?", (user_key,))
            db.execute("DELETE FROM codes WHERE json_extract(data, '$.user_key') = ?", (user_key,))
            db.execute("DELETE FROM grants WHERE user_key = ?", (user_key,))
            db.execute("DELETE FROM refresh_history WHERE user_key = ?", (user_key,))

    @staticmethod
    def _normalize_login(code: str) -> str:
        return code.replace("-", "").replace(" ", "").strip().upper()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if len(client_id) > 256:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT metadata FROM clients WHERE client_id = ? AND (expires_at > ? OR "
                "EXISTS (SELECT 1 FROM grants WHERE grants.client_id = clients.client_id "
                "AND refresh_expires > ?))",
                (client_id, int(time.time()), int(time.time())),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE clients SET expires_at = ? WHERE client_id = ?",
                (int(time.time()) + CLIENT_TTL, client_id),
            )
        return OAuthClientInformationFull.model_validate_json(row["metadata"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if (
            not client_info.redirect_uris
            or len(client_info.redirect_uris) > 10
            or any(not _safe_redirect(str(uri)) for uri in client_info.redirect_uris)
        ):
            raise RegistrationError(
                "invalid_redirect_uri", "Use HTTPS or a literal loopback callback"
            )
        if (
            client_info.token_endpoint_auth_method
            not in ("none", "client_secret_post", "client_secret_basic")
            or "authorization_code" not in client_info.grant_types
            or not set(client_info.grant_types).issubset({"authorization_code", "refresh_token"})
            or client_info.response_types != ["code"]
            or client_info.jwks is not None
            or client_info.jwks_uri is not None
            or len(client_info.model_dump_json()) > 12000
            or any(len(str(value)) > 2048 for value in client_info.model_dump().values() if value)
            or (client_info.client_name is not None and len(client_info.client_name) > 200)
        ):
            raise RegistrationError(
                "invalid_client_metadata", "Unsupported or oversized client metadata"
            )
        stored = client_info.model_copy(update={"client_secret": None})
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            if db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Registration capacity reached")
            db.execute(
                "INSERT INTO clients VALUES (?, ?, ?, ?)",
                (
                    client_info.client_id,
                    stored.model_dump_json(),
                    _hash(client_info.client_secret) if client_info.client_secret else None,
                    int(time.time()) + CLIENT_TTL,
                ),
            )

    async def _register(self, request: Request) -> Response:
        """The SDK DCR handler requires refresh grants even for auth-code-only clients."""
        try:
            body = await request.json()
            metadata = OAuthClientMetadata.model_validate(
                {"grant_types": ["authorization_code"], **body}
            )
            if metadata.scope is not None and not set(metadata.scope.split()).issubset({SCOPE}):
                raise RegistrationError("invalid_client_metadata", "Unsupported scope")
            method = metadata.token_endpoint_auth_method or "client_secret_post"
            info = OAuthClientInformationFull(
                **metadata.model_dump(exclude={"token_endpoint_auth_method", "scope"}),
                token_endpoint_auth_method=method,
                scope=metadata.scope if metadata.scope is not None else SCOPE,
                client_id=_opaque(),
                client_id_issued_at=int(time.time()),
                client_secret=_opaque() if method != "none" else None,
            )
            await self.register_client(info)
        except ValidationError:
            return JSONResponse(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "Invalid client metadata",
                },
                400,
                headers=_SECURITY_HEADERS,
            )
        except RegistrationError as error:
            return JSONResponse(
                {"error": error.error, "error_description": error.error_description},
                400,
                headers=_SECURITY_HEADERS,
            )
        return JSONResponse(
            info.model_dump(mode="json", exclude_none=True), 201, headers=_SECURITY_HEADERS
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self.resource_url:
            raise AuthorizeError("invalid_request", "The resource must equal the MCP resource URL")
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge) is None:
            raise AuthorizeError("invalid_request", "An S256 PKCE challenge is required")
        if params.state is not None and len(params.state) > 1024:
            raise AuthorizeError("invalid_request", "State is too long")
        if str(params.redirect_uri) not in [str(uri) for uri in client.redirect_uris or []]:
            raise AuthorizeError("invalid_request", "Redirect URI is not registered")
        nonce = _opaque()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            if db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] >= MAX_PENDING:
                raise AuthorizeError("temporarily_unavailable", "Connection capacity reached")
            db.execute(
                "INSERT INTO pending (nonce_hash, client_id, params, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    _hash(nonce),
                    client.client_id,
                    params.model_dump_json(),
                    int(time.time()) + LOGIN_TTL,
                ),
            )
        return self.issuer_url + "/oauth/consent?" + urlencode({"request": nonce})

    async def load_authorization_code(self, client, authorization_code):
        if len(authorization_code) > 256:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT data FROM codes WHERE code_hash = ? AND client_id = ? AND expires_at > ?",
                (_hash(authorization_code), client.client_id, int(time.time())),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["data"])
        if self._user(data["user_key"], data["telegram_user_id"]) is None:
            return None
        return _UserAuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(self, client, authorization_code):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM codes WHERE code_hash = ? AND client_id = ? AND expires_at > ?",
                (_hash(authorization_code.code), client.client_id, int(time.time())),
            ).fetchone()
            if row is None:
                raise TokenError("invalid_grant", "Code is invalid or already consumed")
            data = json.loads(row["data"])
            db.execute("DELETE FROM codes WHERE code_hash = ?", (_hash(authorization_code.code),))
            return self._issue(
                db, client.client_id, data["user_key"], data["telegram_user_id"], data["scopes"]
            )

    def _issue(self, db, client_id, user_key, telegram_id, scopes, family_hash=None) -> OAuthToken:
        if self._user(user_key, telegram_id) is None:
            raise TokenError("invalid_grant", "The verified user binding is no longer active")
        self._gc(db)
        if db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] >= MAX_GRANTS:
            raise TokenError("invalid_grant", "Connection capacity reached")
        access, refresh = _opaque(), _opaque()
        now = int(time.time())
        client_row = db.execute(
            "SELECT metadata FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        if client_row is None:
            raise TokenError("invalid_client", "The client registration is no longer active")
        allow_refresh = "refresh_token" in json.loads(client_row["metadata"])["grant_types"]
        db.execute(
            "INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(refresh),
                _hash(access),
                client_id,
                user_key,
                telegram_id,
                json.dumps(scopes),
                self.resource_url,
                now + ACCESS_TTL,
                now + (REFRESH_TTL if allow_refresh else ACCESS_TTL),
                family_hash or _hash(refresh),
            ),
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh if allow_refresh else None,
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(self, client, refresh_token):
        if len(refresh_token) > 256:
            return None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM grants WHERE refresh_hash = ? "
                "AND client_id = ? AND refresh_expires > ?",
                (_hash(refresh_token), client.client_id, int(time.time())),
            ).fetchone()
            if row is None:
                self._revoke_reused_refresh(db, refresh_token, client.client_id)
        if (
            row is None
            or row["resource"] != self.resource_url
            or self._user(row["user_key"], row["telegram_user_id"]) is None
        ):
            return None
        return _UserRefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["refresh_expires"],
            resource=row["resource"],
            subject=row["user_key"],
            user_key=row["user_key"],
            telegram_user_id=row["telegram_user_id"],
        )

    @staticmethod
    def _revoke_reused_refresh(db, token: str, client_id: str) -> None:
        used = db.execute(
            "SELECT family_hash FROM refresh_history WHERE refresh_hash = ? "
            "AND client_id = ? AND expires_at > ?",
            (_hash(token), client_id, int(time.time())),
        ).fetchone()
        if used is not None:
            # A rotated credential was reused: revoke the active token family.
            db.execute("DELETE FROM grants WHERE family_hash = ?", (used["family_hash"],))
            db.execute("DELETE FROM refresh_history WHERE family_hash = ?", (used["family_hash"],))

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM grants WHERE refresh_hash = ? "
                "AND client_id = ? AND refresh_expires > ?",
                (_hash(refresh_token.token), client.client_id, int(time.time())),
            ).fetchone()
            if row is None:
                self._revoke_reused_refresh(db, refresh_token.token, client.client_id)
            else:
                if row["resource"] != self.resource_url or not set(scopes).issubset(
                    json.loads(row["scopes"])
                ):
                    raise TokenError("invalid_grant", "Invalid refresh token audience or scopes")
                self._gc(db)
                if (
                    db.execute("SELECT COUNT(*) FROM refresh_history").fetchone()[0]
                    >= MAX_REFRESH_HISTORY
                ):
                    raise TokenError("invalid_grant", "Connection capacity reached")
                db.execute(
                    "INSERT INTO refresh_history VALUES (?, ?, ?, ?, ?)",
                    (
                        _hash(refresh_token.token),
                        row["family_hash"],
                        row["client_id"],
                        row["user_key"],
                        row["refresh_expires"],
                    ),
                )
                db.execute(
                    "DELETE FROM grants WHERE refresh_hash = ?", (_hash(refresh_token.token),)
                )
                return self._issue(
                    db,
                    client.client_id,
                    row["user_key"],
                    row["telegram_user_id"],
                    scopes,
                    row["family_hash"],
                )
        # Commit replay revocation before returning the protocol error.
        raise TokenError("invalid_grant", "Refresh token is invalid or already consumed")

    async def load_access_token(self, token: str) -> UserAccessToken | None:
        if len(token) > 256:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM grants WHERE access_hash = ? AND access_expires > ?",
                (_hash(token), int(time.time())),
            ).fetchone()
        if (
            row is None
            or row["resource"] != self.resource_url
            or self._user(row["user_key"], row["telegram_user_id"]) is None
        ):
            return None
        return UserAccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["access_expires"],
            resource=row["resource"],
            subject=row["user_key"],
            claims={"iss": self.issuer_url},
            user_key=row["user_key"],
            telegram_user_id=row["telegram_user_id"],
        )

    async def verify_token(self, token: str) -> UserAccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token: UserAccessToken | _UserRefreshToken) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT family_hash FROM grants WHERE client_id = ? "
                "AND (access_hash = ? OR refresh_hash = ?)",
                (token.client_id, _hash(token.token), _hash(token.token)),
            ).fetchone()
            if row is not None:
                db.execute("DELETE FROM grants WHERE family_hash = ?", (row["family_hash"],))
                db.execute(
                    "DELETE FROM refresh_history WHERE family_hash = ?", (row["family_hash"],)
                )

    async def _consent(self, request: Request) -> Response:
        if request.method == "GET":
            nonce = request.query_params.get("request", "")
            csrf = _opaque()
            with self._db() as db:
                self._gc(db)
                row = db.execute(
                    "SELECT * FROM pending WHERE nonce_hash = ?", (_hash(nonce),)
                ).fetchone()
                if row is None or row["attempts"] >= MAX_PENDING_ATTEMPTS:
                    return self._page_error("Запрос истёк. Начните подключение заново.")
                db.execute(
                    "UPDATE pending SET csrf_hash = ? WHERE nonce_hash = ?",
                    (_hash(csrf), _hash(nonce)),
                )
            params = AuthorizationParams.model_validate_json(row["params"])
            callback = urlsplit(str(params.redirect_uri))
            domain = html.escape(callback.netloc)
            headers = dict(_SECURITY_HEADERS)
            # Some browsers apply form-action to the redirect following a form POST.
            # Permit only the validated callback origin in addition to our own form.
            headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
                "form-action 'self';", f"form-action 'self' {callback.scheme}://{callback.netloc};"
            )
            response = HTMLResponse(
                '<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                "<title>AI Connector</title>" + _CONSENT_STYLE + "<body><main>"
                "<h1>AI Connector</h1><h2>Подтвердите связь с вашим чатом</h2>"
                f"<p>Клиент с адресом возврата: <strong>{domain}</strong></p>"
                "<p>Отправьте /connect вашему Telegram-боту и скопируйте одноразовый код.</p>"
                f"<p>{html.escape(PRIVACY_NOTICE)}</p>"
                '<form method="post" action="/oauth/consent">'
                f'<input type="hidden" name="request" value="{html.escape(nonce, quote=True)}">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                '<label>Код подключения из бота <input name="code" required maxlength="80" '
                'autocomplete="off" spellcheck="false"></label>'
                '<button name="decision" value="allow" type="submit">Разрешить подключение</button>'
                '<button class="secondary" name="decision" value="deny" type="submit" '
                "formnovalidate>Отказать</button>"
                "</form></main></body></html>",
                headers=headers,
            )
            response.set_cookie(
                _COOKIE,
                csrf,
                max_age=LOGIN_TTL,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/oauth/consent",
            )
            return response
        # Check Origin and cookie binding before looking up or consuming a Telegram code.
        if request.headers.get("origin") != self.issuer_url:
            return self._page_error("Проверка безопасности не пройдена.", 403)
        form = await request.form()
        nonce, csrf = str(form.get("request", "")), str(form.get("csrf", ""))
        cookie = request.cookies.get(_COOKIE, "")
        if not csrf or not cookie or not hmac.compare_digest(csrf.encode(), cookie.encode()):
            return self._page_error("Проверка безопасности не пройдена.", 403)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._gc(db)
            row = db.execute(
                "SELECT * FROM pending WHERE nonce_hash = ?", (_hash(nonce),)
            ).fetchone()
            if (
                row is None
                or row["attempts"] >= MAX_PENDING_ATTEMPTS
                or not row["csrf_hash"]
                or not hmac.compare_digest(_hash(csrf), row["csrf_hash"])
            ):
                return self._page_error("Запрос истёк или проверка безопасности не пройдена.", 403)
            params = AuthorizationParams.model_validate_json(row["params"])
            if form.get("decision") == "deny":
                db.execute("DELETE FROM pending WHERE nonce_hash = ?", (_hash(nonce),))
                return self._finish(params, error="access_denied")
            if form.get("decision") != "allow":
                return self._page_error("Выберите действие.")
            db.execute(
                "UPDATE pending SET attempts = attempts + 1 WHERE nonce_hash = ?", (_hash(nonce),)
            )
            login = self._normalize_login(str(form.get("code", "")))
            login_row = db.execute(
                "SELECT * FROM login_codes WHERE code_hash = ?", (_hash(login),)
            ).fetchone()
            if (
                login_row is None
                or self._user(login_row["user_key"], login_row["telegram_user_id"]) is None
            ):
                return self._page_error("Код недействителен. Получите новый код командой /connect.")
            if db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] >= MAX_PENDING:
                return self._page_error("Слишком много подключений. Попробуйте позже.", 429)
            code = _opaque()
            data = {
                "client_id": row["client_id"],
                "scopes": params.scopes or [SCOPE],
                "expires_at": int(time.time()) + CODE_TTL,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": self.resource_url,
                "subject": login_row["user_key"],
                "user_key": login_row["user_key"],
                "telegram_user_id": login_row["telegram_user_id"],
            }
            db.execute(
                "INSERT INTO codes VALUES (?, ?, ?, ?)",
                (_hash(code), row["client_id"], json.dumps(data), data["expires_at"]),
            )
            db.execute("DELETE FROM login_codes WHERE user_key = ?", (login_row["user_key"],))
            db.execute("DELETE FROM pending WHERE nonce_hash = ?", (_hash(nonce),))
            return self._finish(params, code=code)

    @staticmethod
    def _page_error(message: str, status: int = 400) -> Response:
        return HTMLResponse(
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>AI Connector</title>"
            + _CONSENT_STYLE
            + f"<body><main><h1>AI Connector</h1><p>{html.escape(message)}</p>"
            "</main></body></html>",
            status,
            headers=_SECURITY_HEADERS,
        )

    @staticmethod
    def _finish(params: AuthorizationParams, **values: str) -> Response:
        response = RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), state=params.state, **values),
            302,
            headers=_SECURITY_HEADERS,
        )
        response.delete_cookie(
            _COOKIE, path="/oauth/consent", secure=True, httponly=True, samesite="lax"
        )
        return response

    def create_routes(self) -> list[Route]:
        options = ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        )
        revocation = RevocationOptions(enabled=True)
        issuer = AnyHttpUrl(self.issuer_url)
        routes = create_auth_routes(
            self,
            issuer,
            client_registration_options=options,
            revocation_options=revocation,
        )
        authenticator = _HashedClientAuthenticator(self)
        handlers = {
            "/token": TokenHandler(self, authenticator).handle,
            "/revoke": RevocationHandler(self, authenticator).handle,
            "/register": self._register,
        }
        metadata = build_metadata(issuer, None, options, revocation)
        metadata.token_endpoint_auth_methods_supported = [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ]
        metadata.revocation_endpoint_auth_methods_supported = (
            metadata.token_endpoint_auth_methods_supported
        )
        metadata.client_id_metadata_document_supported = False

        async def metadata_response(request: Request) -> Response:
            return JSONResponse(
                metadata.model_dump(mode="json", exclude_none=True),
                headers={"Cache-Control": "no-store"},
            )

        for route in routes:
            if route.path == "/.well-known/oauth-authorization-server":
                route.app = cors_middleware(metadata_response, ["GET", "OPTIONS"])
                continue
            if route.path in handlers:
                route.app = cors_middleware(handlers[route.path], ["POST", "OPTIONS"])
            route.app = _RequestGuard(route.app, self, route.path)
        consent = Route("/oauth/consent", self._consent, methods=["GET", "POST"])
        consent.app = _RequestGuard(request_response(self._consent), self, consent.path)
        routes.append(consent)
        return routes
