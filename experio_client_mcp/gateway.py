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
import contextvars
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time

import anyio

from experio_client_mcp import audit, config, experio
from experio_client_mcp.limits import Capacity, Failures, Rates

MCP_PATH = re.compile(r"/([a-z][a-z0-9_]{0,63})/mcp/?")
CHALLENGE = [(b"www-authenticate", b'Bearer realm="experio"')]
CAPACITY = contextvars.ContextVar("capacity")  # the Capacity this request's tools run their threads in
MAX_CREDENTIAL = 256  # characters of a login or a key


def parse_credentials(header):
    """Return (login, key) from an ``Authorization`` header value (Bearer or Basic, base64 of login:key), or None."""
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() not in ("bearer", "basic") or not token.strip():
        return None
    token = token.strip().replace("-", "+").replace("_", "/")  # base64 or its url-safe form
    try:
        text = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode()
    except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
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


def capped(receive):
    """Return ``receive`` ending the body after MAX_BODY bytes (a chunked body has no Content-Length to check)."""
    total = 0

    async def limited():
        nonlocal total
        message = await receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > config.MAX_BODY:
                return {"type": "http.request", "body": b"", "more_body": False}
        return message

    return limited


class Gateway:
    """ASGI middleware: authenticates a request for a served cabinet, then hands it to ``app`` as /mcp."""

    def __init__(self, app, cabinets, failures=None, rates=None, clock=time.monotonic):
        self.app, self.cabinets, self.clock = app, cabinets, clock
        self.failures, self.rates = failures or Failures(clock), rates or Rates(clock)
        self.secret = secrets.token_bytes(32)  # fingerprints the credentials in memory: nothing durable
        self.logins = {}  # fingerprint -> (Session, expires)
        self.capacity = Capacity()
        self.slots, self.locks, self.waiting = (
            None,
            {},
            0,
        )  # logins tried at once (made in the loop), one lock per login

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
        """Return who the request comes from: the address the proxy itself added (the last of X-Forwarded-For) when the
        proxy is trusted, else the peer's; an IPv6 address stands for its /64, which one machine owns whole."""
        forwarded = headers.get(b"x-forwarded-for") if config.TRUST_PROXY else None
        raw = forwarded.decode("latin-1").split(",")[-1].strip() if forwarded else (scope.get("client") or ("?", 0))[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return raw[:64]
        return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else raw

    async def sign_in(self, cabinet, login, key, source):
        """Return (session, None), or (None, the refusal to send: status, body, headers). One login is tried at a time
        (a second request for it finds the first one's answer), a few at once in all; wrong credentials count BEFORE
        the next try, so a burst cannot get past the pause."""
        mark, session = self.known(cabinet, login, key)
        if session:  # a login remembered is served even while its address or login is paused
            return session, None
        who = (cabinet, login.lower())
        lock = self.locks.setdefault(who, [anyio.Lock(), 0])
        lock[1] += 1
        try:
            async with lock[0]:
                session = self.known(cabinet, login, key)[1]  # a request for the same login may have just signed in
                if session:
                    return session, None
                return await self.try_login(cabinet, login, key, mark, source, who)
        finally:
            lock[1] -= 1
            if not lock[1]:
                del self.locks[who]

    async def try_login(self, cabinet, login, key, mark, source, who):
        paused = (429, {"error": "too many failed attempts, try later"}, [(b"retry-after", str(config.PAUSE).encode())])
        if self.failures.paused(source) or self.failures.paused(who):
            return None, paused
        if self.waiting >= config.AUTH_WAITING:
            return None, (503, {"error": "server busy"}, [(b"retry-after", b"5")])
        self.slots = self.slots or anyio.Semaphore(config.AUTH_CONCURRENT)
        self.waiting += 1
        try:
            async with self.slots:
                if self.failures.paused(source) or self.failures.paused(who):
                    return None, paused
                uid = await anyio.to_thread.run_sync(experio.authenticate, cabinet, self.cabinets[cabinet], login, key)
        except experio.Unreachable:
            return None, (503, {"error": "Experio is not reachable"}, [])
        finally:
            self.waiting -= 1
        if not uid:
            started = [self.failures.failed(source), self.failures.failed(who)]
            if any(started):
                audit.event("paused", cabinet=cabinet, user=audit.fingerprint(cabinet, login))
            audit.event("login_refused", cabinet=cabinet, user=audit.fingerprint(cabinet, login))
            return None, (401, {"error": "login or key refused"}, CHALLENGE)
        self.failures.cleared(who)
        session = experio.Session(cabinet, self.cabinets[cabinet], login, key, uid)
        self.remember(mark, session)
        return session, None

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
        if config.TRUST_PROXY and headers.get(b"x-forwarded-proto") != b"https":
            return await respond(send, 403, {"error": "https only"})
        if scope["method"] != "POST":
            return await respond(send, 405, {"error": "POST only"}, [(b"allow", b"POST")])
        if int(headers.get(b"content-length") or 0) > config.MAX_BODY:
            return await respond(send, 413, {"error": "request too large"})
        credentials = parse_credentials(headers.get(b"authorization", b"").decode("latin-1"))
        if not credentials:
            return await respond(send, 401, {"error": "login and key required"}, CHALLENGE)
        login, key = credentials
        session, refusal = await self.sign_in(cabinet, login, key, self.address(scope, headers))
        if refusal:
            return await respond(send, *refusal)
        user = (cabinet, login.lower())
        refused = self.rates.start(user)
        if refused:
            audit.event("limited", cabinet=cabinet, user=audit.fingerprint(cabinet, login), why=refused)
            return await respond(
                send,
                429,
                {"error": "too many requests" if refused == "rate" else "too many calls at once"},
                [(b"retry-after", b"10")],
            )
        tokens = experio.SESSION.set(session), CAPACITY.set(self.capacity)
        try:
            await self.app({**scope, "path": "/mcp", "raw_path": b"/mcp"}, capped(receive), send)
        finally:
            CAPACITY.reset(tokens[1])
            experio.SESSION.reset(tokens[0])
            self.rates.end(user)
        return None
