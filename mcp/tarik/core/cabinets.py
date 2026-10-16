"""The cabinets Tarik serves, and the one the current call is for.

A cabinet is named by its Experio database (experio_cabinet_8). The same name is its
Postgres schema (its files, results, questions, mail). A setting is either the cabinet's own
(CABINET_SETTINGS: its Experio login, its mailbox's credentials and addresses, its n8n agent) or
shared by all of them (SHARED_SETTINGS: the mail server, n8n's MCP token, the model), never both.

They come from Vault (tarik.core.vault: secret/tarik/shared, secret/tarik/cabinets/<cabinet>, the
cabinets being those paths). The tests set them in the environment instead: TARIK_CABINETS lists the
cabinets, a cabinet's own settings are prefixed with its name (EXPERIO_CABINET_8__ODOO_UID).

Settings stay on the server: no tool returns one, and only a ToolError's own message reaches
the agent (tests/core/test_cabinets.py checks every tool).

Each call names its cabinet (the URL /c/<cabinet>/mcp, see server.py), and everything
cabinet-specific goes through current(): with no cabinet set it fails. There is no
default cabinet, so a call can only ever work in the one it named."""

import contextlib
import contextvars
import os
import re
from pathlib import Path
from typing import Final

import psycopg

from tarik.core import vault
from tarik.core.config import POSTGRES

# A lowercase letter, then up to 62 lowercase letters, digits or underscores: a Postgres schema name and an env
# prefix as it is, with no quoting nor case change.
NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


def cabinet_names(value):
    """TARIK_CABINETS (comma-separated) as a frozenset of names, checked: each exactly a lowercase identifier,
    no repeats; SystemExit otherwise."""
    names = [name.strip() for name in (value or "").split(",") if name.strip()]
    for name in names:
        if not NAME.fullmatch(name) or name == "public" or name.startswith("pg_"):
            raise SystemExit(f"TARIK_CABINETS: {name!r} is not a cabinet's exact Experio database name")
    if len(set(names)) != len(names):
        raise SystemExit("TARIK_CABINETS lists a cabinet twice")
    return frozenset(names)


CABINETS: Final = cabinet_names(",".join(vault.settings(wait=60)[1]) if vault.ENABLED else os.getenv("TARIK_CABINETS"))
_current = contextvars.ContextVar("cabinet")


def current():
    """The cabinet of this call. Fails outside one: nothing cabinet-specific may run unscoped."""
    try:
        return _current.get()
    except LookupError:
        raise RuntimeError("no cabinet for this call") from None


@contextlib.contextmanager
def using(name):
    """Run the block for cabinet ``name``, one of TARIK_CABINETS."""
    if name not in CABINETS:
        raise PermissionError(f"unknown cabinet {name!r}")
    token = _current.set(name)
    try:
        yield name
    finally:
        _current.reset(token)


# Each cabinet's own: Vault secret/tarik/cabinets/<cabinet>, or env <CABINET>__<KEY>.
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
        "AGENT_ID",
    ]
)
# The same for every cabinet: Vault secret/tarik/shared, or env <KEY>.
SHARED_SETTINGS = frozenset(
    [
        "IMAP_HOST",
        "IMAP_PORT",
        "IMAP_FOLDER",
        "SMTP_HOST",
        "SMTP_PORT",
        "MAIL_CERT_SHA256",
        "N8N_MCP_API_TOKEN",
        "MODEL_API_KEY",
        "MODEL_API_URL",
        "MODEL_API_NAME",
        "MAIL_WEBHOOK_SECRET",
    ]
)


def source():
    """Every setting, as one mapping: <KEY> for a shared one, <CABINET>__<KEY> for a cabinet's own."""
    if not vault.ENABLED:
        return os.environ
    shared, cabinets = vault.settings()
    return {
        **shared,
        **{f"{name.upper()}__{key}": value for name, own in cabinets.items() for key, value in own.items()},
    }


def setting(key, default=None):
    """Setting ``key`` for the current cabinet: its own value (env <CABINET>__<KEY>) for a cabinet
    setting, never another cabinet's nor an unprefixed one; the common value (env <KEY>) for a
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
    found = [f"tarik/shared:{key}" for key in shared if key not in SHARED_SETTINGS]
    found += [
        f"tarik/cabinets/{name}:{key}" for name, own in cabinets.items() for key in own if key not in CABINET_SETTINGS
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
