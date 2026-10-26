"""The cabinets Tarik serves, and the one the current call is for.

A cabinet is named by its Experio database (experio_cabinet_8). The same name is its
Postgres schema (its files, results, questions, mail). A setting is either the cabinet's own
(CABINET_SETTINGS: its Experio login, its mailbox's credentials and addresses) or shared by all of them
(SHARED_SETTINGS: the mail server; the model's key and address, the orchestrator's own), never both.

Settings come from Vault (VAULT_ADDR): secret/tarik/shared and secret/tarik/orchestrator hold the shared ones,
secret/tarik/cabinets/<cabinet> each cabinet's own, and the cabinets are the paths listed there.
The tests set them in the environment instead: TARIK_CABINETS lists the cabinets, and a cabinet's own
settings are prefixed with its name (EXPERIO_CABINET_8__ODOO_UID).

Settings stay on the server: no tool returns one, and only a ToolError's own message reaches
the agent (tests/isolation/test_no_setting_shown.py checks every tool).

Each call names its cabinet (engine/calls.py runs it in using(cabinet)), and everything
cabinet-specific goes through current(): with no cabinet set it fails. There is no
default cabinet, so a call can only ever work in the one it named.

The orchestrator logs in with tarik-mcp's AppRole (role_id and secret_id files in /run/vault, vault/setup.sh), whose
policy only reads secret/tarik/*. Everything is read at start, then again every 5 minutes, so a
changed secret applies without a restart. A cabinet created through the Experio API is served at once
(server/experio_api.py reloads CABINETS and creates its schema); one added in Vault by hand needs a restart, since
schemas are otherwise created at start. When Vault can't be reached, the last values stay in use. Without
VAULT_ADDR, nothing is read here and the settings come from the environment: the tests only (server/server.py
refuses to start)."""

import contextlib
import contextvars
import os
import re
import threading
import time
from pathlib import Path
from typing import Final

import httpx
import psycopg
from psycopg import sql

from engine.tools.core.config import POSTGRES
from engine.tools.core.queries import Q

# --- Vault ---

VAULT_ADDRESS = os.getenv("VAULT_ADDR", "").rstrip("/")
VAULT_ENABLED = bool(VAULT_ADDRESS)
VAULT_APPROLE = "/run/vault"  # the AppRole files, mounted by docker-compose.yml
VAULT_REFRESH_SECONDS = 300
VAULT_RETRY_SECONDS = 30

_vault_lock = threading.Lock()
_vault_state = {"token": "", "token_until": 0.0, "values": None, "next_read": 0.0}


def vault_request(method, path, token="", **kwargs):
    """Return Vault's JSON answer (dict) to HTTP ``method`` on ``path``, sent with ``token`` when given; None for a
    404. Raises httpx.HTTPStatusError for any other error status."""
    headers = {"X-Vault-Token": token} if token else {}
    answer = httpx.request(method, VAULT_ADDRESS + path, headers=headers, timeout=10, **kwargs)
    if answer.status_code == 404:
        return None
    answer.raise_for_status()
    return answer.json()


def vault_token(role, state):
    """Return a Vault token for the AppRole whose role_id and secret_id files are in directory ``role``.

    The token is cached in ``state`` (its "token" and "token_until" keys) and a new login is made when it is
    missing or once 80% of its lease has passed."""
    if not state["token"] or time.monotonic() > state["token_until"]:
        ids = {name: Path(role, name).read_text().strip() for name in ("role_id", "secret_id")}
        auth = vault_request("POST", "/v1/auth/approle/login", json=ids)["auth"]
        state.update(token=auth["client_token"], token_until=time.monotonic() + 0.8 * auth["lease_duration"])
    return state["token"]


def _vault_read(method, path):
    """Return Vault's answer to ``method`` on ``path`` (as vault_request), using tarik-mcp's AppRole token."""
    return vault_request(method, path, vault_token(VAULT_APPROLE, _vault_state))


def _vault_field(method, path, key):
    """Return ``key`` of the "data" in Vault's answer to ``method`` on ``path``; None when path or key is missing."""
    answer = _vault_read(method, path) or {}
    return (answer.get("data") or {}).get(key)


def read_vault():
    """Read every setting from Vault. Returns (shared, cabinets): shared is {key: value} from secret/tarik/shared and
    secret/tarik/orchestrator (the latter wins); cabinets is {cabinet: {key: value}} for each cabinet path."""
    shared = _vault_field("GET", "/v1/secret/data/tarik/shared", "data") or {}
    own = _vault_field("GET", "/v1/secret/data/tarik/orchestrator", "data") or {}
    shared = {**shared, **own}
    listed = _vault_field("LIST", "/v1/secret/metadata/tarik/cabinets", "keys") or []
    cabinets = {}
    for name in listed:
        if name.endswith("/"):
            continue
        cabinets[name] = _vault_read("GET", f"/v1/secret/data/tarik/cabinets/{name}")["data"]["data"]
    return shared, cabinets


def _refresh_vault(wait):
    """Read every setting again into _vault_state, retrying for ``wait`` seconds on a first read (then raising).
    Once values were read, a Vault outage keeps the last ones and the next read is tried VAULT_RETRY_SECONDS
    later."""
    deadline = time.monotonic() + wait
    while True:
        try:
            _vault_state["values"] = read_vault()
            _vault_state["next_read"] = time.monotonic() + VAULT_REFRESH_SECONDS
            return
        except Exception as error:
            if _vault_state["values"] is not None:  # Vault out for a while: carry on with what was read
                print(f"vault: {type(error).__name__}; the last values stay in use", flush=True)
                _vault_state["next_read"] = time.monotonic() + VAULT_RETRY_SECONDS
                return
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def vault_settings(wait=0):
    """Return (shared, cabinets) as read_vault does, read again once VAULT_REFRESH_SECONDS old. ``wait``: seconds
    to keep trying a first read (Vault still starting or unsealing)."""
    with _vault_lock:
        if _vault_state["values"] is None or time.monotonic() >= _vault_state["next_read"]:
            _refresh_vault(wait)
        return _vault_state["values"]


def reread_vault():
    """Read every setting again now (after a change made from the Cabinets page), in use from the next call."""
    with _vault_lock:
        _refresh_vault(0)


# --- The cabinets ---

# A lowercase letter, then up to 62 lowercase letters, digits or underscores: a Postgres schema name and an env
# prefix as it is, with no quoting nor case change.
NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


def cabinet_names(value):
    """Return the comma-separated cabinet names in ``value`` (Vault's cabinet paths, or TARIK_CABINETS in the tests)
    as a frozenset. Raises SystemExit unless each is a lowercase identifier (NAME, not "public" nor "pg_...") and
    none is repeated."""
    names = [name.strip() for name in (value or "").split(",") if name.strip()]
    for name in names:
        if not NAME.fullmatch(name) or name == "public" or name.startswith("pg_"):
            raise SystemExit(f"TARIK_CABINETS: {name!r} is not a cabinet's exact Experio database name")
    if len(set(names)) != len(names):
        raise SystemExit("TARIK_CABINETS lists a cabinet twice")
    return frozenset(names)


def active(own):
    """True when a cabinet's own settings ``own`` (dict) let Tarik serve it. ACTIVE "0" marks a cabinet deactivated
    through the Experio API's delete, its settings and data kept."""
    return own.get("ACTIVE", "1") != "0"


def served():
    """Return the cabinets to serve now (frozenset): Vault's cabinet paths except the deactivated ones; in the tests,
    TARIK_CABINETS."""
    if not VAULT_ENABLED:
        return cabinet_names(os.getenv("TARIK_CABINETS"))
    _shared, cabinets = vault_settings(wait=60)
    return cabinet_names(",".join(name for name, own in cabinets.items() if active(own)))


# The cabinets served, one set changed in place (reload_cabinets) so that every module that imported it sees a
# cabinet created, reactivated or deactivated through the Experio API at once.
CABINETS: Final = set(served())


def reload_cabinets():
    """Read Vault again and update CABINETS in place. Returns (added, removed): sets of cabinet names."""
    reread_vault()
    now = served()
    added, removed = now - CABINETS, CABINETS - now
    CABINETS.difference_update(removed)
    CABINETS.update(added)
    return added, removed


_current = contextvars.ContextVar("cabinet")


def current():
    """Return the cabinet of this call. Raises RuntimeError outside one: nothing cabinet-specific may run unscoped."""
    try:
        return _current.get()
    except LookupError:
        raise RuntimeError("no cabinet for this call") from None


@contextlib.contextmanager
def using(name):
    """Context manager: run the block for cabinet ``name``; PermissionError unless it is one of CABINETS."""
    if name not in CABINETS:
        raise PermissionError(f"unknown cabinet {name!r}")
    token = _current.set(name)
    try:
        yield name
    finally:
        _current.reset(token)


# Each cabinet's own: Vault secret/tarik/cabinets/<cabinet> (the tests: env <CABINET>__<KEY>).
CABINET_SETTINGS = frozenset(
    [
        "ODOO_UID",
        "ODOO_USER",
        "ODOO_PASSWORD",
        "ODOO_API_KEY",
        "DRY_RUN",
        "IMAP_USER",
        "IMAP_PASSWORD",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "MAIL_MANAGER",
        "MAIL_ALLOWED_RECIPIENTS",
        "ACTIVE",  # "0": deactivated by the Experio API (missing: served)
    ]
)
# The same for every cabinet: Vault secret/tarik/shared (the tests: env <KEY>).
SHARED_SETTINGS = frozenset(
    [
        "IMAP_HOST",
        "IMAP_PORT",
        "IMAP_FOLDER",
        "SMTP_HOST",
        "SMTP_PORT",
        "MAIL_CERT_SHA256",
        "MODEL_API_KEY",  # secret/tarik/orchestrator
        "MODEL_API_URL",  # secret/tarik/orchestrator, optional (the provider's Anthropic-style address)
        "CLAUDE_CODE_OAUTH_TOKEN",  # secret/tarik/orchestrator, optional: the claude CLI's login (`claude setup-token`)
        "EXPERIO_API_KEY",  # secret/tarik/orchestrator: what the Experio app shows to create, change or close cabinets
        "GLADDEN_URL",  # optional: Experio's Gladden classifier (its runner's /gladden)
        "GLADDEN_CALLBACK_URL",  # optional: this server's /gladden as Gladden reaches it
    ]
)


def source():
    """Return every setting as one mapping: <KEY> for a shared one, <CABINET>__<KEY> for a cabinet's own."""
    if not VAULT_ENABLED:
        return os.environ
    shared, cabinets = vault_settings()
    every = dict(shared)
    for name, own in cabinets.items():
        for key, value in own.items():
            every[f"{name.upper()}__{key}"] = value
    return every


def setting(key, default=None):
    """Return setting ``key`` for the current cabinet: for a cabinet setting, the cabinet's own value
    (<CABINET>__<KEY> in source()), never another cabinet's nor an unprefixed one; for a shared one, <KEY>.
    An unset or empty setting returns ``default``. Raises KeyError for an unknown key, or an unset one without
    default."""
    if key in CABINET_SETTINGS:
        value = source().get(f"{current().upper()}__{key}")
    elif key in SHARED_SETTINGS:
        current()  # shared, but still only within a cabinet's call
        value = source().get(key)
    else:
        raise KeyError(f"unknown setting {key}")
    if value is None or value == "":
        if default is None:
            raise KeyError(f"{current()}: {key} is not set")  # stays on the server: not a ToolError
        return default
    return value


def misplaced_in_vault(shared, cabinets, environ=os.environ):
    """Return the misplaced settings (sorted list of "path:key" or environment names), with Vault: a key under the
    wrong Vault path or unknown, or a setting still in ``environ``, where it would be a second copy. The server
    refuses to start with any."""
    found = [f"tarik/shared:{key}" for key in shared if key not in SHARED_SETTINGS]
    for name, own in cabinets.items():
        found += [f"tarik/cabinets/{name}:{key}" for key in own if key not in CABINET_SETTINGS]
    known = CABINET_SETTINGS | SHARED_SETTINGS
    # TARIK_CABINETS, a setting (<KEY>) or a cabinet's own (<CABINET>__<KEY>), set in the environment.
    for name, value in environ.items():
        _cabinet, _, key = name.partition("__")
        if value and (name == "TARIK_CABINETS" or name in known or key in known):
            found.append(name)
    return sorted(found)


def cabinet_db():
    """Return a connection to Tarik's Postgres that only sees the current cabinet's schema: an unqualified
    table name can only be that cabinet's (search_path holds nothing else, not even public)."""
    return psycopg.connect(**POSTGRES, options=f"-c search_path={current()}")


def create_schemas():
    """Create each served cabinet's schema and tables if missing (engine/sql/cabinet_schema.sql is idempotent)."""
    for name in sorted(CABINETS):
        create_schema(name)


def create_schema(name):
    """Create cabinet ``name``'s schema and tables if missing."""
    with psycopg.connect(**POSTGRES) as admin:
        admin.execute(sql.SQL(Q.create_cabinet_schema).format(name=sql.Identifier(name)))
    with using(name), cabinet_db() as conn:
        conn.execute(Q.cabinet_schema)
