"""The cabinets Tarik serves, and the one the current call is for.

A cabinet is named by its Experio database (experio_cabinet_8). The same name is its
Postgres schema (its files, results, questions, mail). A setting is either the cabinet's own
(CABINET_SETTINGS: its Experio login, its mailbox's credentials and addresses) or shared by all of them
(SHARED_SETTINGS: the mail server; the model's key and address, the orchestrator's own), never both.

They come from Vault (secret/tarik/shared, secret/tarik/orchestrator, secret/tarik/cabinets/<cabinet>, the
cabinets being those paths). The n8n stack reads the same Vault: its own keys (N8N_ONLY) are left alone.
The tests set them in the environment instead: TARIK_CABINETS lists the cabinets, a cabinet's own
settings are prefixed with its name (EXPERIO_CABINET_8__ODOO_UID).

Settings stay on the server: no tool returns one, and only a ToolError's own message reaches
the agent (tests/isolation/test_no_setting_shown.py checks every tool).

Each call names its cabinet (engine/tools.py runs it in using(cabinet)), and everything
cabinet-specific goes through current(): with no cabinet set it fails. There is no
default cabinet, so a call can only ever work in the one it named.

Tarik's settings in Vault (VAULT_ADDR): secret/tarik/shared holds the shared ones, and
secret/tarik/cabinets/<cabinet> each cabinet's own; the cabinets are those paths.

The orchestrator logs in with tarik-mcp's AppRole (role_id and secret_id files in /run/vault, vault/setup.sh), whose
policy only reads secret/tarik/*. Everything is read at start, then again every 5 minutes, so a
changed secret applies without a restart (a new cabinet still needs one: its schema is made at
start). When Vault can't be reached, the last values stay in use. Without VAULT_ADDR, nothing is
read here and the settings come from the environment: the tests only (server.py refuses to start)."""

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

from tarik.core.config import POSTGRES

# --- Vault ---

VAULT_ADDRESS = os.getenv("VAULT_ADDR", "").rstrip("/")
VAULT_ENABLED = bool(VAULT_ADDRESS)
VAULT_APPROLE = "/run/vault"  # the AppRole files, mounted by docker-compose.yml
VAULT_REFRESH_SECONDS = 300
VAULT_RETRY_SECONDS = 30

_vault_lock = threading.Lock()
_vault_state = {"token": "", "token_until": 0.0, "values": None, "next_read": 0.0}


def _vault_request(method, path, token="", **kwargs):
    """Vault's JSON answer (dict) to ``method`` on ``path``, with ``token`` when given; None for a 404."""
    headers = {"X-Vault-Token": token} if token else {}
    answer = httpx.request(method, VAULT_ADDRESS + path, headers=headers, timeout=10, **kwargs)
    if answer.status_code == 404:
        return None
    answer.raise_for_status()
    return answer.json()


def _vault_read(method, path):
    """Vault's answer to ``method`` on ``path`` (as _vault_request), logged in first when the token is missing
    or old."""
    if not _vault_state["token"] or time.monotonic() > _vault_state["token_until"]:  # logged in with the AppRole's ids
        ids = {name: Path(VAULT_APPROLE, name).read_text().strip() for name in ("role_id", "secret_id")}
        auth = _vault_request("POST", "/v1/auth/approle/login", json=ids)["auth"]
        # The token is kept, renewed once 80% of its lease has passed.
        _vault_state.update(token=auth["client_token"], token_until=time.monotonic() + 0.8 * auth["lease_duration"])
    return _vault_request(method, path, _vault_state["token"])


def read_vault():
    """Vault's vault_settings: (the shared ones, {cabinet: its own})."""
    shared = ((_vault_read("GET", "/v1/secret/data/tarik/shared") or {}).get("data") or {}).get("data") or {}
    own = ((_vault_read("GET", "/v1/secret/data/tarik/orchestrator") or {}).get("data") or {}).get("data") or {}
    shared = {**shared, **own}
    listed = ((_vault_read("LIST", "/v1/secret/metadata/tarik/cabinets") or {}).get("data") or {}).get("keys") or []
    cabinets = {
        name: _vault_read("GET", f"/v1/secret/data/tarik/cabinets/{name}")["data"]["data"]
        for name in listed
        if not name.endswith("/")
    }
    return shared, cabinets


def _refresh_vault(wait):
    """Read every setting again, trying for ``wait`` seconds on a first read. Once read, an outage keeps the last
    values, tried again VAULT_RETRY_SECONDS later."""
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
    """The shared and per-cabinet vault_settings, read again once they are 5 minutes old. ``wait``: seconds
    to keep trying a first read (Vault still starting or unsealing)."""
    with _vault_lock:
        if _vault_state["values"] is None or time.monotonic() >= _vault_state["next_read"]:
            _refresh_vault(wait)
        return _vault_state["values"]


# --- The cabinets ---

# A lowercase letter, then up to 62 lowercase letters, digits or underscores: a Postgres schema name and an env
# prefix as it is, with no quoting nor case change.
NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


def cabinet_names(value):
    """The cabinets' names, comma-separated (Vault's cabinet paths, or TARIK_CABINETS in the tests), as a frozenset,
    checked: each exactly a lowercase identifier, no repeats; SystemExit otherwise."""
    names = [name.strip() for name in (value or "").split(",") if name.strip()]
    for name in names:
        if not NAME.fullmatch(name) or name == "public" or name.startswith("pg_"):
            raise SystemExit(f"TARIK_CABINETS: {name!r} is not a cabinet's exact Experio database name")
    if len(set(names)) != len(names):
        raise SystemExit("TARIK_CABINETS lists a cabinet twice")
    return frozenset(names)


CABINETS: Final = cabinet_names(",".join(vault_settings(wait=60)[1]) if VAULT_ENABLED else os.getenv("TARIK_CABINETS"))
_current = contextvars.ContextVar("cabinet")


def current():
    """The cabinet of this call. Fails outside one: nothing cabinet-specific may run unscoped."""
    try:
        return _current.get()
    except LookupError:
        raise RuntimeError("no cabinet for this call") from None


@contextlib.contextmanager
def using(name):
    """Run the block for cabinet ``name``, one of CABINETS."""
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
        "MAIL_BY_ORCHESTRATOR",  # secret/tarik/orchestrator: the cabinets whose mailbox this stack answers
    ]
)
# The n8n stack's, in the same Vault paths: neither read nor refused here.
N8N_ONLY = frozenset(["AGENT_ID", "N8N_MCP_API_TOKEN", "MAIL_WEBHOOK_SECRET"])


def source():
    """Every setting, as one mapping: <KEY> for a shared one, <CABINET>__<KEY> for a cabinet's own."""
    if not VAULT_ENABLED:
        return os.environ
    shared, cabinets = vault_settings()
    return {
        **shared,
        **{f"{name.upper()}__{key}": value for name, own in cabinets.items() for key, value in own.items()},
    }


def setting(key, default=None):
    """Setting ``key`` for the current cabinet: its own value (<CABINET>__<KEY> in source()) for a
    cabinet setting, never another cabinet's nor an unprefixed one; the common value (<KEY>) for a
    shared one. A missing setting without default is an error."""
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
    """With Vault: a setting under the wrong path, unknown, or still in the environment, where it
    would be a second copy. The server refuses to start with any."""
    found = [f"tarik/shared:{key}" for key in shared if key not in SHARED_SETTINGS | N8N_ONLY]
    found += [
        f"tarik/cabinets/{name}:{key}"
        for name, own in cabinets.items()
        for key in own
        if key not in CABINET_SETTINGS | N8N_ONLY
    ]
    known = CABINET_SETTINGS | SHARED_SETTINGS
    # TARIK_CABINETS, a setting (<KEY>) or a cabinet's own (<CABINET>__<KEY>), set in the environment.
    found += [
        name
        for name, value in environ.items()
        if value and (name == "TARIK_CABINETS" or name in known or name.partition("__")[2] in known)
    ]
    return sorted(found)


SCHEMA = Path(__file__).with_name("cabinet.sql")


def cabinet_db():
    """A connection to Tarik's Postgres that only sees the current cabinet's schema: an unqualified
    table name can only be that cabinet's (search_path holds nothing else, not even public)."""
    return psycopg.connect(**POSTGRES, options=f"-c search_path={current()}")


def create_schemas():
    """Each cabinet's schema and tables, created if missing (cabinet.sql is idempotent)."""
    ddl = SCHEMA.read_text()
    for name in sorted(CABINETS):
        with psycopg.connect(**POSTGRES) as admin:
            admin.execute(f"create schema if not exists {name}")
        with using(name), cabinet_db() as conn:
            conn.execute(ddl)
