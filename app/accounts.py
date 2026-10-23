"""Who may use the team's pages: each person's account (email, name, password kept as an scrypt hash, never the
password), and the sessions they opened (a random token in an HttpOnly cookie; only its hash is kept here). Accounts
are made by an admin, on the Comptes page or from a terminal (app/users.py): there is no sign-up page. One made on the
page has no password until its person sets one, through a one-time link (an invitation, INVITATION_HOURS long; only
its token's hash is kept). Each has a role (ROLES): an admin changes what
everyone uses, a member reads and talks to Tarik (app/server.py's admin_only says which is which)."""

import hashlib
import hmac
import re
import secrets
import time

from engine import runs
from tarik.core.queries import Q

SESSION_HOURS = 12
ROLES = ("admin", "member")
INVITATION_HOURS = 72
MIN_PASSWORD = 12
EMAIL = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")  # a comma never: it separates a list's addresses
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


async def last_admin(db, email):
    """Whether ``email`` is the only open admin account: demoted or closed, nobody could manage the others."""
    admins = [row[0] for row in await (await db.execute(Q.admins_active)).fetchall()]
    return admins == [email]


async def set_role(email, role):
    """An account's role changed (one of ROLES); LookupError when there is no such account, ValueError for the last
    admin made a member."""
    if role not in ROLES:
        raise ValueError(f"role {role!r}: admin or member")
    email = email.strip().lower()
    async with runs.pool.connection() as db:
        if role != "admin" and await last_admin(db, email):
            raise ValueError("c'est le dernier administrateur : nommez-en un autre d'abord")
        if not await (await db.execute(Q.user_role, (role, email))).fetchone():
            raise LookupError(f"no account {email}")


async def disable(email):
    """An account closed: it can no longer log in, its sessions end and its links die. ValueError for the last
    admin."""
    email = email.strip().lower()
    async with runs.pool.connection() as db:
        if await last_admin(db, email):
            raise ValueError("c'est le dernier administrateur : nommez-en un autre d'abord")
        await db.execute(Q.user_closed, (email,))
        await db.execute(Q.user_sessions_ended, (email,))
        await db.execute(Q.invitations_removed, (email,))


async def listed():
    """Every account: {email, name, role, active, created, has_password, last_seen (its latest session's end)}."""
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.users_listed)).fetchall()
    keys = ("email", "name", "role", "active", "created", "has_password", "last_seen")
    return [dict(zip(keys, row, strict=True)) for row in rows]


async def invite(email, name, role):
    """A new account, without a password, and its invitation's token; ValueError (in plain French) when the email or
    the name won't do, or the account exists (make it a new link instead)."""
    email, name = email.strip().lower(), name.strip()
    if not EMAIL.fullmatch(email):
        raise ValueError("ce n'est pas une adresse email")
    if not name:
        raise ValueError("le nom est vide")
    if role not in ROLES:
        raise ValueError("rôle inconnu")
    async with runs.pool.connection() as db:
        if not await (await db.execute(Q.user_invited, (email, name, role))).fetchone():
            raise ValueError("ce compte existe déjà : donnez-lui un nouveau lien")
    return await new_link(email)


async def new_link(email):
    """A new one-time link's token for this account (a new account's first password, or a reset); the links it had
    die. The password it has works until the link is used. LookupError when there is no such account."""
    email, token = email.strip().lower(), secrets.token_urlsafe(32)
    async with runs.pool.connection() as db:
        if not await (await db.execute(Q.user_found, (email,))).fetchone():
            raise LookupError(f"no account {email}")
        await db.execute(Q.invitations_removed, (email,))
        await db.execute(Q.invitation_made, (digest(token), email, INVITATION_HOURS))
    return token


async def invited(token):
    """{email, name} of the account a link is for while it can be used, else None."""
    async with runs.pool.connection() as db:
        row = await (await db.execute(Q.invitation_found, (digest(token or ""),))).fetchone()
    return {"email": row[0], "name": row[1]} if row else None


async def accept(token, password):
    """The password its person chose, through the link ``token`` (used once): the account open, its other sessions
    ended. The email (str); ValueError when the password won't do or the link can't be used."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"mot de passe trop court : {MIN_PASSWORD} caractères au moins")
    async with runs.pool.connection() as db, db.transaction():
        row = await (await db.execute(Q.invitation_used, (digest(token or ""),))).fetchone()
        if not row:
            raise ValueError("ce lien a déjà servi ou n'est plus valable : demandez-en un nouveau")
        await db.execute(Q.user_password_set, (hashed(password), row[0]))
        await db.execute(Q.user_sessions_ended, (row[0],))
    return row[0]


def forget_waited(now):
    """The failures past their wait forgotten: nothing to remember, the throttle never grows without end."""
    for stale in [key for key, (_, at) in FAILURES.items() if now - at >= WAIT_SECONDS]:
        del FAILURES[stale]


async def log_in(email, password):
    """A new session's token for this email and password, or None (wrong, closed, or too many tries)."""
    email = email.strip().lower()
    count, last = FAILURES.get(email, (0, 0.0))
    if count >= WAIT_AFTER and time.monotonic() - last < WAIT_SECONDS:
        return None
    async with runs.pool.connection() as db:
        row = await (await db.execute(Q.user_password, (email,))).fetchone()
        if not row or not row[0] or not matches(password, row[0]):  # no password yet: its invitation sets one
            now = time.monotonic()
            forget_waited(now)
            FAILURES[email] = (count + 1, now)
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
    """{email, name, role} of the session ``token`` opened, or None when it is unknown, expired or its account
    closed."""
    if not token:
        return None
    async with runs.pool.connection() as db:
        row = await (await db.execute(Q.session_user, (digest(token),))).fetchone()
    return {"email": row[0], "name": row[1], "role": row[2]} if row else None


async def log_out(token):
    async with runs.pool.connection() as db:
        await db.execute(Q.session_closed, (digest(token or ""),))
