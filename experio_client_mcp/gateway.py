"""The door of the server, in front of the MCP application. A request is POST /<cabinet>/mcp with the client's own
Experio login and key (``Authorization: Bearer <base64 of login:key>``, or Basic). The order is the safeguard:

1. the cabinet is looked up first: one that is not served is a plain 404, and the credentials are never read for it;
2. the credentials are tried in THAT cabinet's Experio database only, so a client of another cabinet cannot get in by
   changing the url: their login does not exist there;
3. wrong credentials are counted by login and by address and then paused (so this is no way to guess a password nor to
   lock someone's account out of Experio), a right login is remembered for a few minutes, and each credential has a
   rate and a number of calls at once;
4. only then the MCP application runs, with the session in a context variable, and nothing is kept after the request."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time

import anyio

from experio_client_mcp import audit, config, experio
from experio_client_mcp.limits import Failures, Rates

MCP_PATH = re.compile(r"/([a-z][a-z0-9_]{0,63})/mcp/?")
MAX_CREDENTIAL = 256  # characters of a login or a key
UNDECODABLE = (binascii.Error, UnicodeDecodeError, ValueError)  # a tuple: the module runs on Python 3.10 and later


def parse_credentials(header):
    """Return (login, key) from an ``Authorization`` header value (Bearer or Basic, base64 of login:key), or None."""
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() not in ("bearer", "basic") or not token.strip():
        return None
    token = token.strip().replace("-", "+").replace("_", "/")  # base64 or its url-safe form
    try:
        text = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode()
    except UNDECODABLE:
        return None
    login, colon, key = text.partition(":")
    if not colon or not login or not key or max(len(login), len(key)) > MAX_CREDENTIAL:
        return None
    if not (login + key).isprintable():
        return None
    return login, key


async def respond(send, status, body, headers=()):
    data = json.dumps(body).encode()
    start = {"type": "http.response.start", "status": status}
    start["headers"] = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"), *headers]
    await send(start)
    await send({"type": "http.response.body", "body": data})


class Gateway:
    """ASGI middleware: authenticates a request for a served cabinet, then hands it to ``app`` as /mcp."""

    def __init__(self, app, cabinets, failures=None, rates=None, clock=time.monotonic):
        self.app, self.cabinets, self.clock = app, cabinets, clock
        self.failures, self.rates = failures or Failures(clock), rates or Rates(clock)
        self.secret = secrets.token_bytes(32)  # fingerprints the credentials in memory: nothing durable
        self.logins = {}  # fingerprint -> (Session, expires)

    def known(self, cabinet, login, key):
        """Return the fingerprint of a credential and the session remembered for it, None when there is none."""
        mark = hmac.new(self.secret, "\0".join((cabinet, login, key)).encode(), hashlib.sha256).hexdigest()
        found = self.logins.get(mark)
        if found and found[1] > self.clock():
            return mark, found[0]
        self.logins.pop(mark, None)
        return mark, None

    def remember(self, mark, session):
        if len(self.logins) > 2000:  # a bounded memory: drop what has expired, then the oldest
            now = self.clock()
            self.logins = {m: v for m, v in self.logins.items() if v[1] > now}
            while len(self.logins) > 1000:
                self.logins.pop(next(iter(self.logins)))
        self.logins[mark] = (session, self.clock() + config.LOGIN_TTL)

    def address(self, scope, headers):
        if config.TRUST_PROXY and headers.get(b"x-forwarded-for"):
            return headers[b"x-forwarded-for"].decode().split(",")[-1].strip()  # the one the proxy itself added
        return (scope.get("client") or ("?", 0))[0]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return None
        headers = dict(scope["headers"])
        if scope["path"] == "/healthz":
            return await respond(send, 200, {"status": "ok"})
        found = MCP_PATH.fullmatch(scope["path"])
        if not found or found[1] not in self.cabinets:
            return await respond(send, 404, {"error": "not found"})  # before the credentials are even read
        cabinet = found[1]
        if config.TRUST_PROXY and headers.get(b"x-forwarded-proto", b"https") != b"https":
            return await respond(send, 403, {"error": "https only"})
        if scope["method"] != "POST":
            return await respond(send, 405, {"error": "POST only"}, [(b"allow", b"POST")])
        credentials = parse_credentials(headers.get(b"authorization", b"").decode("latin-1"))
        challenge = [(b"www-authenticate", b'Bearer realm="experio"')]
        if not credentials:
            return await respond(send, 401, {"error": "login and key required"}, challenge)
        login, key = credentials
        source, who = self.address(scope, headers), audit.fingerprint(cabinet, login)
        if self.failures.paused(source) or self.failures.paused((cabinet, login)):
            audit.event("paused", cabinet=cabinet, user=who)
            return await respond(
                send,
                429,
                {"error": "too many failed attempts, try later"},
                [(b"retry-after", str(config.PAUSE).encode())],
            )
        mark, session = self.known(cabinet, login, key)
        if session is None:
            try:
                uid = await anyio.to_thread.run_sync(experio.authenticate, cabinet, self.cabinets[cabinet], login, key)
            except experio.Unreachable:
                return await respond(send, 503, {"error": "Experio is not reachable"})
            if not uid:
                self.failures.failed(source)
                self.failures.failed((cabinet, login))
                audit.event("login_refused", cabinet=cabinet, user=who)
                return await respond(send, 401, {"error": "login or key refused"}, challenge)
            self.failures.cleared((cabinet, login))
            session = experio.Session(cabinet, self.cabinets[cabinet], login, key, uid)
            self.remember(mark, session)
        refused = self.rates.start(mark)
        if refused:
            audit.event("limited", cabinet=cabinet, user=who, why=refused)
            return await respond(
                send,
                429,
                {"error": "too many requests" if refused == "rate" else "too many calls at once"},
                [(b"retry-after", b"10")],
            )
        token = experio.SESSION.set(session)
        try:
            await self.app({**scope, "path": "/mcp", "raw_path": b"/mcp"}, receive, send)
        finally:
            experio.SESSION.reset(token)
            self.rates.end(mark)
        return None
