"""Manage who may use the team's pages: accounts (email, name, password kept only as an scrypt hash) and the sessions
they open (a random token in an HttpOnly cookie; only its hash is stored).

An admin creates accounts on the Comptes page or from a terminal (server/users.py): there is no sign-up page. An
account created on the page has no password until its person sets one through a one-time link (an invitation valid
INVITATION_HOURS; only its token's hash is stored). Each account has a role (ROLES): an admin changes what everyone
uses, a member reads and talks to Tarik (server/server.py's admin_only decides which routes need an admin)."""

import hashlib
import hmac
import re
import secrets
import time

from engine import runs
from engine.tools.core.queries import Q

SESSION_HOURS = 12
ROLES = ("admin", "member")
INVITATION_HOURS = 72
MIN_PASSWORD = 12
EMAIL = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")  # a comma never: it separates a list's addresses
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
FAILURES = {}  # email: (count, last failure's monotonic time): a guessing run waits
WAIT_AFTER, WAIT_SECONDS = 5, 60


def hashed(password, salt=None):
    """Return ``password``'s scrypt hash as "scrypt$<salt hex>$<hash hex>", with ``salt`` (bytes) or a new random one.
    Return ``password``'s scrypt hash as "scrypt$<salt hex>$<hash hex>", with ``salt`` (bytes) or a new random one."""
    salt = salt or secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def matches(password, stored):
    """True when ``password`` hashes to ``stored`` (a hashed() string), compared in constant time."""
    _, salt, _ = stored.split("$")
    return hmac.compare_digest(hashed(password, bytes.fromhex(salt)), stored)


def digest(token):
    """Return the SHA-256 hex digest of ``token``: the only form in which session and link tokens are stored."""
    return hashlib.sha256(token.encode()).hexdigest()


async def save_user(email, name, password):
    """Create an account, or update an existing one's name and password and reactivate it."""
    async with runs.pool.connection() as db:
        await db.execute(
            Q.user_saved,
            (email.strip().lower(), name.strip(), hashed(password)),
        )


async def last_admin(db, email):
    """True when ``email`` is the only active admin account: demoting or closing it would leave nobody to manage
    accounts."""
    admins = [row[0] for row in await (await db.execute(Q.admins_active)).fetchall()]
    return admins == [email]


async def set_role(email, role):
    """Change the role (one of ROLES) of the account ``email``.

    Raises:
        ValueError: an unknown role, or the last admin made a member.
        LookupError: no such account."""
    if role not in ROLES:
        raise ValueError(f"role {role!r}: admin or member")
    email = email.strip().lower()
    async with runs.pool.connection() as db:
        if role != "admin" and await last_admin(db, email):
            raise ValueError("c'est le dernier administrateur : nommez-en un autre d'abord")
        if not await (await db.execute(Q.user_role, (role, email))).fetchone():
            raise LookupError(f"no account {email}")


async def disable(email):
    """Close the account ``email``: it can no longer log in, its sessions end and its links are deleted. ValueError for
    the last admin."""
    email = email.strip().lower()
    async with runs.pool.connection() as db:
        if await last_admin(db, email):
            raise ValueError("c'est le dernier administrateur : nommez-en un autre d'abord")
        await db.execute(Q.user_closed, (email,))
        await db.execute(Q.user_sessions_ended, (email,))
        await db.execute(Q.invitations_removed, (email,))


async def listed():
    """Return every account: [{email, name, role, active, created, has_password, last_seen}], last_seen being its latest
    session's expiry."""
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.users_listed)).fetchall()
    keys = ("email", "name", "role", "active", "created", "has_password", "last_seen")
    return [dict(zip(keys, row, strict=True)) for row in rows]


async def invite(email, name, role):
    """Create an account without a password and return its invitation link's token.

    Raises ValueError (in plain French) for an invalid email, an empty name, an unknown role, or an existing account
    (give it a new link instead)."""
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
    """Create a new one-time link for the account ``email`` (a new account's first password, or a reset) and return its
    token.

    The account's earlier links are deleted; its current password keeps working until the link is used. Raises
    LookupError when there is no such account."""
    email, token = email.strip().lower(), secrets.token_urlsafe(32)
    async with runs.pool.connection() as db:
        if not await (await db.execute(Q.user_found, (email,))).fetchone():
            raise LookupError(f"no account {email}")
        await db.execute(Q.invitations_removed, (email,))
        await db.execute(Q.invitation_made, (digest(token), email, INVITATION_HOURS))
    return token


async def invited(token):
    """Return {email, name} of the account a link ``token`` is for while the link is usable, else None."""
    async with runs.pool.connection() as db:
        row = await (await db.execute(Q.invitation_found, (digest(token or ""),))).fetchone()
    return {"email": row[0], "name": row[1]} if row else None


async def accept(token, password):
    """Set the password chosen through the one-time link ``token`` and return the account's email.

    Using the link spends it, reactivates the account and ends its other sessions. Raises ValueError when the
    password is shorter than MIN_PASSWORD or the link is used or expired."""
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
    """Drop the login failures whose wait is over (at ``now``, monotonic), so FAILURES never grows without bound."""
    for stale in [key for key, (_, at) in FAILURES.items() if now - at >= WAIT_SECONDS]:
        del FAILURES[stale]


async def log_in(email, password):
    """Open a session for ``email`` and ``password`` and return its token, or None.

    None when the password is wrong, the account has no password yet or is closed, or after WAIT_AFTER failures
    within WAIT_SECONDS (a guessing run waits)."""
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
    """Return {email, name, role} of the session ``token`` opened, or None when it is unknown, expired or its account
    closed."""
    if not token:
        return None
    async with runs.pool.connection() as db:
        row = await (await db.execute(Q.session_user, (digest(token),))).fetchone()
    return {"email": row[0], "name": row[1], "role": row[2]} if row else None


async def log_out(token):
    """End the session ``token`` opened."""
    async with runs.pool.connection() as db:
        await db.execute(Q.session_closed, (digest(token or ""),))
