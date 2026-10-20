"""Who may use the team's pages: each person's account (email, name, password kept as an scrypt hash, never the
password), and the sessions they opened (a random token in an HttpOnly cookie; only its hash is kept here). Accounts
are made from a terminal (users.py): there is no sign-up page."""

import hashlib
import hmac
import secrets
import time

from engine import runs
from tarik.core.queries import Q

SESSION_HOURS = 12
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
FAILURES = {}  # email: (count, last failure, monotonic): a guessing run waits
WAIT_AFTER, WAIT_SECONDS = 5, 60


def hashed(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def matches(password, stored):
    _, salt, _ = stored.split("$")
    return hmac.compare_digest(hashed(password, bytes.fromhex(salt)), stored)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def save_user(email, name, password):
    """An account made, or its name and password changed (and reactivated)."""
    async with runs.pool.connection() as db:
        await db.execute(
            Q.user_saved,
            (email.strip().lower(), name.strip(), hashed(password)),
        )


async def disable(email):
    """An account closed: it can no longer log in, and its sessions end."""
    async with runs.pool.connection() as db:
        await db.execute(Q.user_closed, (email.strip().lower(),))
        await db.execute(Q.user_sessions_ended, (email.strip().lower(),))


async def log_in(email, password):
    """A new session's token for this email and password, or None (wrong, closed, or too many tries)."""
    email = email.strip().lower()
    count, last = FAILURES.get(email, (0, 0.0))
    if count >= WAIT_AFTER and time.monotonic() - last < WAIT_SECONDS:
        return None
    async with runs.pool.connection() as db:
        row = await (await db.execute(Q.user_password, (email,))).fetchone()
        if not row or not matches(password, row[0]):
            FAILURES[email] = (count + 1, time.monotonic())
            return None
        FAILURES.pop(email, None)
        token = secrets.token_urlsafe(32)
        await db.execute(
            Q.session_opened,
            (digest(token), email, SESSION_HOURS),
        )
        await db.execute(Q.sessions_expired_removed)
    return token


async def user_of(token):
    """{email, name} of the session ``token`` opened, or None when it is unknown, expired or its account closed."""
    if not token:
        return None
    async with runs.pool.connection() as db:
        row = await (
            await db.execute(
                Q.session_user,
                (digest(token),),
            )
        ).fetchone()
    return {"email": row[0], "name": row[1]} if row else None


async def log_out(token):
    async with runs.pool.connection() as db:
        await db.execute(Q.session_closed, (digest(token or ""),))
