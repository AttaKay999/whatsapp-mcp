"""Minimal single-owner OAuth 2.1 authorization server for the MCP endpoint.

Lets clients such as the Gemini web app connect with a normal OAuth sign-in:
  - discovery:  /.well-known/oauth-protected-resource, /.well-known/oauth-authorization-server
  - /register   dynamic client registration (RFC 7591)
  - /authorize  password login page for the owner + PKCE (S256)
  - /token      authorization_code and refresh_token grants
Tokens, codes and clients live in SQLite on the persistent volume, so connections
survive restarts. Secrets are stored only as SHA-256 hashes.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store")
DB_PATH = os.path.join(STORE_DIR, "oauth.db")
CODE_TTL = 10 * 60
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
LOGIN_LIMIT = 5
LOGIN_WINDOW = 10 * 60


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(expected, challenge)


def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = (
        "authorization, content-type, accept, mcp-protocol-version, mcp-session-id, last-event-id")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, HEAD, OPTIONS"
    resp.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate, Mcp-Session-Id"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def _json(data: Dict[str, Any], status: int = 200) -> Response:
    resp = JSONResponse(data, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return _cors(resp)


def _log(msg: str) -> None:
    print(f"[oauth] {msg}", flush=True)


def _oauth_error(error: str, description: str, status: int = 400) -> Response:
    _log(f"{status} {error}: {description}")
    return _json({"error": error, "error_description": description}, status)


def _redirect_shape_ok(uri: str) -> bool:
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment:
        return False
    if u.scheme == "https" and u.netloc:
        return True
    if u.scheme == "http":
        return u.hostname in ("localhost", "127.0.0.1", "::1")
    blocked = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp"}
    return bool(u.scheme) and u.scheme not in blocked and bool(u.path or u.netloc)


class OAuth:
    def __init__(self, base_url: str, password: str, db_path: str = DB_PATH,
                 static_client_id: str = "", static_client_secret: str = ""):
        if not password:
            raise ValueError("OAUTH_PASSWORD is required")
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.db_path = db_path
        self.static_client_id = static_client_id
        self.static_client_secret = static_client_secret
        self._fails: Dict[str, List[float]] = {}
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY, secret_hash TEXT, redirect_uris TEXT NOT NULL,
                    name TEXT, auth_method TEXT NOT NULL DEFAULT 'none', created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codes (
                    code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
                    challenge TEXT NOT NULL, scope TEXT, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
                    scope TEXT, expires_at REAL NOT NULL);
                """
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ---------- metadata ----------
    def resource_metadata_url(self, suffix: str = "mcp") -> str:
        return f"{self.base_url}/.well-known/oauth-protected-resource/{suffix}"

    async def protected_resource(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _cors(Response(status_code=204))
        path = request.url.path.rstrip("/")
        resource = (f"{self.base_url}/mcp" if path.endswith("/mcp") else
                    f"{self.base_url}/sse" if path.endswith("/sse") else self.base_url)
        return _json({
            "resource": resource,
            "authorization_servers": [self.base_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp", "openid", "offline_access"],
            "resource_name": "WhatsApp MCP",
        })

    async def auth_server(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _cors(Response(status_code=204))
        return _json({
            "issuer": self.base_url,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": f"{self.base_url}/token",
            **({} if os.environ.get("OAUTH_DISABLE_DCR") == "1" else
               {"registration_endpoint": f"{self.base_url}/register"}),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": ["mcp", "openid", "offline_access"],
            "revocation_endpoint": f"{self.base_url}/revoke",
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        })

    async def revoke(self, request: Request) -> Response:
        """RFC 7009: always answers 200 so tokens cannot be probed."""
        if request.method == "OPTIONS":
            return _cors(Response(status_code=204))
        raw = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        token = (raw.get("token") or [""])[0]
        if token:
            with self._conn() as c:
                c.execute("DELETE FROM tokens WHERE token_hash=?", (_hash(token),))
        return _json({})

    # ---------- clients ----------
    def _client(self, client_id: str) -> Optional[Dict[str, Any]]:
        if not client_id:
            return None
        if self.static_client_id and hmac.compare_digest(client_id, self.static_client_id):
            return {"client_id": client_id, "static": True, "name": "Pre-configured client",
                    "auth_method": "client_secret_post" if self.static_client_secret else "none",
                    "secret_hash": _hash(self.static_client_secret) if self.static_client_secret else None,
                    "redirect_uris": []}
        with self._conn() as c:
            row = c.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["redirect_uris"] = json.loads(d["redirect_uris"])
        d["static"] = False
        d["auth_method"] = d["auth_method"]
        return d

    @staticmethod
    def _redirect_allowed(client: Dict[str, Any], uri: str) -> bool:
        if not _redirect_shape_ok(uri):
            return False
        return True if client.get("static") else uri in client["redirect_uris"]

    async def register(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _cors(Response(status_code=204))
        try:
            body = await request.json()
            assert isinstance(body, dict)
        except Exception:
            return _oauth_error("invalid_client_metadata", "Body must be a JSON object")
        _log(f"register request: name={body.get('client_name')!r} redirect_uris={body.get('redirect_uris')!r} "
             f"auth_method={body.get('token_endpoint_auth_method')!r}")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and _redirect_shape_ok(u) for u in uris):
            return _oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty list of https (or localhost) URIs")
        method = body.get("token_endpoint_auth_method") or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return _oauth_error("invalid_client_metadata", "Unsupported token_endpoint_auth_method")
        name = str(body.get("client_name") or "MCP client")[:100]
        client_id = "c_" + secrets.token_urlsafe(16)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        with self._conn() as c:
            c.execute("INSERT INTO clients VALUES (?,?,?,?,?,?)",
                      (client_id, _hash(secret) if secret else None, json.dumps(uris), name, method, time.time()))
        out: Dict[str, Any] = {
            "client_id": client_id, "client_id_issued_at": int(time.time()), "redirect_uris": uris,
            "client_name": name, "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        }
        if secret:
            out["client_secret"] = secret
            out["client_secret_expires_at"] = 0
        return _json(out, 201)

    # ---------- authorize ----------
    def _locked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._fails.get(ip, []) if now - t < LOGIN_WINDOW]
        self._fails[ip] = recent
        return len(recent) >= LOGIN_LIMIT

    @staticmethod
    def _ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")

    def _page(self, client: Dict[str, Any], params: Dict[str, str], error: str = "", status: int = 200) -> Response:
        host = urlsplit(params["redirect_uri"]).netloc
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in params.items()
        )
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect WhatsApp MCP</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#15171a;--mut:#5b6470;--acc:#128c4a;--err:#c0392b;--bd:#dfe3e8}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1114;--card:#181b20;--fg:#eef0f2;--mut:#9aa3ad;--acc:#2fbf71;--err:#ff7a6b;--bd:#2a2f36}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:16px;box-sizing:border-box}}
.card{{width:100%;max-width:400px;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:24px}}
h1{{font-size:20px;margin:0 0 6px}}p{{margin:0 0 14px;color:var(--mut);font-size:14px}}
b{{color:var(--fg)}}input[type=password]{{width:100%;box-sizing:border-box;padding:12px;font-size:16px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg)}}
button{{margin-top:14px;width:100%;padding:12px;font-size:16px;font-weight:600;border:0;border-radius:8px;background:var(--acc);color:#fff;cursor:pointer}}
.err{{color:var(--err)}}
</style></head><body><form class="card" method="post" action="/authorize">
<h1>Connect to your WhatsApp MCP</h1>
<p><b>{html.escape(client.get("name") or "An app")}</b> ({html.escape(host)}) wants to read and send WhatsApp messages as you. Only approve if you started this.</p>
{err}{hidden}
<input type="password" name="password" placeholder="Your password" autocomplete="current-password" autofocus required>
<button type="submit">Approve</button></form></body></html>"""
        return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @staticmethod
    def _fail_page(message: str, status: int = 400) -> Response:
        return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                            f"<body style='font:16px system-ui;padding:24px'><h3>Cannot continue</h3><p>{html.escape(message)}</p>",
                            status_code=status)

    async def authorize(self, request: Request) -> Response:
        if request.method == "POST":
            raw = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        else:
            raw = parse_qs(request.url.query, keep_blank_values=True)
        p = {k: v[0] for k, v in raw.items() if v}
        client = self._client(p.get("client_id", ""))
        redirect_uri = p.get("redirect_uri", "")
        _log(f"authorize {request.method}: client={p.get('client_id')!r} redirect_uri={redirect_uri!r} "
             f"response_type={p.get('response_type')!r} method={p.get('code_challenge_method')!r}")
        if not client:
            _log("authorize refused: unknown client")
            return self._fail_page("Unknown client.")
        if not redirect_uri or not self._redirect_allowed(client, redirect_uri):
            _log("authorize refused: redirect_uri not allowed")
            return self._fail_page("Invalid redirect URI.")

        def bounce(error: str, desc: str) -> Response:
            q = {"error": error, "error_description": desc, "iss": self.base_url}
            if p.get("state"):
                q["state"] = p["state"]
            return RedirectResponse(redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(q), status_code=302)

        if p.get("response_type") != "code":
            return bounce("unsupported_response_type", "Only response_type=code is supported")
        if not p.get("code_challenge") or p.get("code_challenge_method", "S256") != "S256":
            return bounce("invalid_request", "PKCE with code_challenge_method=S256 is required")

        keep = {k: p.get(k, "") for k in ("client_id", "redirect_uri", "response_type", "code_challenge",
                                          "code_challenge_method", "state", "scope", "resource")}
        keep["code_challenge_method"] = "S256"
        keep = {k: v for k, v in keep.items() if v}
        if request.method == "GET":
            return self._page(client, keep)

        ip = self._ip(request)
        if self._locked(ip):
            return self._page(client, keep, "Too many wrong attempts. Try again in 10 minutes.", 429)
        if not hmac.compare_digest(p.get("password", "").encode(), self.password.encode()):
            self._fails.setdefault(ip, []).append(time.time())
            return self._page(client, keep, "Wrong password.", 401)

        code = secrets.token_urlsafe(32)
        with self._conn() as c:
            c.execute("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
            c.execute("INSERT INTO codes VALUES (?,?,?,?,?,?)",
                      (_hash(code), client["client_id"], redirect_uri, p["code_challenge"], "mcp", time.time() + CODE_TTL))
        q = {"code": code, "iss": self.base_url}
        if p.get("state"):
            q["state"] = p["state"]
        return RedirectResponse(redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(q), status_code=302)

    # ---------- token ----------
    def _issue(self, c: sqlite3.Connection, client_id: str) -> Dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        c.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
        c.execute("INSERT INTO tokens VALUES (?,?,?,?,?)", (_hash(access), "access", client_id, "mcp", now + ACCESS_TTL))
        c.execute("INSERT INTO tokens VALUES (?,?,?,?,?)", (_hash(refresh), "refresh", client_id, "mcp", now + REFRESH_TTL))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": "mcp"}

    async def token(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _cors(Response(status_code=204))
        raw = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        f = {k: v[0] for k, v in raw.items() if v}
        _log(f"token request: grant={f.get('grant_type')!r} client={f.get('client_id')!r}")
        client_id, client_secret = f.get("client_id", ""), f.get("client_secret", "")
        basic = request.headers.get("authorization", "")
        if basic.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(basic[6:]).decode().partition(":")
                client_id, client_secret = client_id or user, client_secret or pw
            except Exception:
                return _oauth_error("invalid_client", "Bad basic auth header", 401)
        client = self._client(client_id)
        if not client:
            return _oauth_error("invalid_client", "Unknown client", 401)
        if client.get("secret_hash") and not hmac.compare_digest(_hash(client_secret), client["secret_hash"]):
            return _oauth_error("invalid_client", "Bad client secret", 401)

        grant = f.get("grant_type")
        if grant == "authorization_code":
            code, verifier = f.get("code", ""), f.get("code_verifier", "")
            if not code or not verifier:
                return _oauth_error("invalid_request", "code and code_verifier are required")
            with self._conn() as c:
                row = c.execute("SELECT * FROM codes WHERE code_hash=?", (_hash(code),)).fetchone()
                if row:  # single use, even if validation below fails
                    c.execute("DELETE FROM codes WHERE code_hash=?", (_hash(code),))
                if (not row or row["expires_at"] < time.time() or row["client_id"] != client["client_id"]
                        or (f.get("redirect_uri") and f["redirect_uri"] != row["redirect_uri"])
                        or not _pkce_ok(verifier, row["challenge"])):
                    return _oauth_error("invalid_grant", "Invalid, expired or already used authorization code")
                return _json(self._issue(c, client["client_id"]))
        if grant == "refresh_token":
            rt = f.get("refresh_token", "")
            with self._conn() as c:
                row = c.execute("SELECT * FROM tokens WHERE token_hash=? AND kind='refresh'", (_hash(rt),)).fetchone()
                if not row or row["expires_at"] < time.time() or row["client_id"] != client["client_id"]:
                    return _oauth_error("invalid_grant", "Invalid or expired refresh token")
                # Google expects a stable refresh token: keep it, extend its life, issue a new access token
                now = time.time()
                c.execute("UPDATE tokens SET expires_at=? WHERE token_hash=?", (now + REFRESH_TTL, _hash(rt)))
                c.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
                access = secrets.token_urlsafe(32)
                c.execute("INSERT INTO tokens VALUES (?,?,?,?,?)",
                          (_hash(access), "access", client["client_id"], "mcp", now + ACCESS_TTL))
                return _json({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                              "refresh_token": rt, "scope": "mcp"})
        return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token")

    def verify_access(self, token: str) -> bool:
        if not token:
            return False
        with self._conn() as c:
            row = c.execute("SELECT expires_at FROM tokens WHERE token_hash=? AND kind='access'", (_hash(token),)).fetchone()
        return bool(row and row["expires_at"] > time.time())

    def build_app(self) -> Starlette:
        wk = ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
              "/.well-known/oauth-protected-resource/sse"]
        asm = ["/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp",
               "/.well-known/openid-configuration", "/.well-known/openid-configuration/mcp"]
        routes = [Route(p, self.protected_resource, methods=["GET", "OPTIONS"]) for p in wk]
        routes += [Route(p, self.auth_server, methods=["GET", "OPTIONS"]) for p in asm]
        routes += [
            Route("/register", self.register, methods=["POST", "OPTIONS"]),
            Route("/authorize", self.authorize, methods=["GET", "POST"]),
            Route("/token", self.token, methods=["POST", "OPTIONS"]),
            Route("/revoke", self.revoke, methods=["POST", "OPTIONS"]),
        ]
        return Starlette(routes=routes)


class AuthLayer:
    """ASGI wrapper: serves OAuth endpoints and requires a valid credential for everything else.

    Accepted credentials: the static MCP_AUTH_TOKEN (OpenCode, Gemini CLI) or an OAuth access token.
    /media/<random>/<file> links stay open because the random link is the secret.
    """

    def __init__(self, app, static_token: Optional[str] = None, oauth: Optional[OAuth] = None):
        self.app = app
        self.static_token = static_token.encode() if static_token else None
        self.oauth = oauth
        self.oauth_app = oauth.build_app() if oauth else None

    def _allowed(self, auth_header: str) -> bool:
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:].strip()
        if self.static_token and hmac.compare_digest(token.encode(), self.static_token):
            return True
        return bool(self.oauth and self.oauth.verify_access(token))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        alias = path.rstrip("/") == "/sse"
        if alias:  # same server answers on /sse as on /mcp
            scope = dict(scope, path="/mcp", raw_path=b"/mcp")
            path = "/mcp"
        if path.startswith("/media/"):
            await self.app(scope, receive, send)
            return
        if self.oauth_app and (path.startswith("/.well-known/") or path in ("/register", "/authorize", "/token", "/revoke")):
            await self.oauth_app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":  # CORS preflight must never need credentials
            await _cors(Response(status_code=204))(scope, receive, send)
            return
        auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        if not auth:
            print(f"[auth] unauthenticated {scope.get('method')} {path} -> 401", flush=True)
        if self._allowed(auth):
            await self.app(scope, receive, send)
            return
        # drain the request body (and log a sample of what the caller sent) before refusing
        chunks, more = [], True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        h = dict(scope["headers"])
        print(
            f"[auth] probe {scope.get('method')} {path} ua={h.get(b'user-agent', b'').decode('latin-1')[:80]!r} "
            f"accept={h.get(b'accept', b'').decode('latin-1')[:80]!r} ctype={h.get(b'content-type', b'').decode('latin-1')[:60]!r} "
            f"body={b''.join(chunks)[:400]!r}",
            flush=True,
        )
        challenge = "Bearer"
        if self.oauth:
            challenge = f'Bearer resource_metadata="{self.oauth.resource_metadata_url("sse" if alias else "mcp")}"'
        body = json.dumps({"error": "unauthorized", "error_description": "Sign in required"}).encode()
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"access-control-allow-origin", b"*"),
            (b"access-control-expose-headers", b"WWW-Authenticate"),
            (b"www-authenticate", challenge.encode())]})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})
