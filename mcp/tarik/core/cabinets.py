"""The cabinets Tarik serves, and the one the current call is for.

A cabinet is named by its Experio database (experio_cabinet_8). The same name is its
Postgres schema (its files, results, questions, mail) and the prefix of its own settings in
the environment (EXPERIO_CABINET_8__ODOO_UID). TARIK_CABINETS lists them all. A setting is
either the cabinet's own (CABINET_SETTINGS: its Experio login, its mailbox's credentials and
addresses, its n8n agent) or shared by all of them (SHARED_SETTINGS: the mail server, n8n's MCP
server), never both.

Settings stay on the server: no tool returns one, and only a ToolError's own message reaches
the agent (tests/core/test_cabinets.py checks every tool).

Each call names its cabinet (the URL /c/<cabinet>/mcp, see server.py), and everything
cabinet-specific goes through current(): with no cabinet set it fails. There is no
default cabinet, so a call can only ever work in the one it named."""

import contextlib
import contextvars
import os
import re

import psycopg

NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")  # a Postgres schema and an env prefix as it is: no quoting, no case change


def cabinet_names(value):
    """TARIK_CABINETS, checked: each name exactly a lowercase identifier, no repeats."""
    names = [name.strip() for name in (value or "").split(",") if name.strip()]
    for name in names:
        if not NAME.fullmatch(name) or name == "public" or name.startswith("pg_"):
            raise SystemExit(f"TARIK_CABINETS: {name!r} is not a cabinet's exact Experio database name")
    if len(set(names)) != len(names):
        raise SystemExit("TARIK_CABINETS lists a cabinet twice")
    return frozenset(names)


CABINETS = cabinet_names(os.getenv("TARIK_CABINETS"))
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


# Each cabinet's own: env <CABINET>__<KEY>.
CABINET_SETTINGS = frozenset({"ODOO_UID", "ODOO_USER", "ODOO_PASSWORD", "ODOO_API_KEY", "DRY_RUN",
                              "IMAP_USER", "IMAP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
                              "MAIL_MANAGER", "MAIL_ALLOWED_RECIPIENTS", "AGENT_ID"})
# The same for every cabinet: env <KEY>.
SHARED_SETTINGS = frozenset({"IMAP_HOST", "IMAP_PORT", "IMAP_FOLDER", "SMTP_HOST", "SMTP_PORT", "MAIL_CERT_SHA256",
                             "N8N_MCP_API_URL", "N8N_MCP_API_TOKEN"})


def setting(key, default=None):
    """Setting ``key`` for the current cabinet: its own value (env <CABINET>__<KEY>) for a cabinet
    setting, never another cabinet's nor an unprefixed one; the common value (env <KEY>) for a
    shared one. A missing setting without default is an error."""
    if key in CABINET_SETTINGS:
        value = os.getenv(f"{current().upper()}__{key}")
    elif key in SHARED_SETTINGS:
        current()  # shared, but still only within a cabinet's call
        value = os.getenv(key)
    else:
        raise KeyError(f"unknown setting {key}")
    if value is None or value == "":
        if default is None:
            raise KeyError(f"{current()}: {key} is not set")  # stays on the server: not a ToolError
        return default
    return value


def misplaced_settings(environ=os.environ):
    """Settings set where they don't belong: a cabinet setting without its cabinet's prefix (it
    would be nobody's), a shared one under a cabinet's prefix (it would be ignored), or a prefix
    of no listed cabinet. The server refuses to start with any."""
    found = sorted(key for key in CABINET_SETTINGS if environ.get(key))
    for name, value in environ.items():
        prefix, separator, key = name.partition("__")
        if separator and value and (key in CABINET_SETTINGS or key in SHARED_SETTINGS) and prefix.isupper():
            if key in SHARED_SETTINGS or prefix.lower() not in CABINETS:
                found.append(name)
    return sorted(found)


POSTGRES = {"host": os.getenv("POSTGRES_HOST", "postgres"), "dbname": os.getenv("POSTGRES_DB"),
            "user": os.getenv("POSTGRES_USER"), "password": os.getenv("POSTGRES_PASSWORD")}
SCHEMA = os.path.join(os.path.dirname(__file__), "cabinet.sql")


def cabinet_db():
    """A connection to Tarik's Postgres that only sees the current cabinet's schema: an unqualified
    table name can only be that cabinet's (search_path holds nothing else, not even public)."""
    return psycopg.connect(**POSTGRES, options=f"-c search_path={current()}")


def create_schemas():
    """Each cabinet's schema and tables, created if missing (cabinet.sql is idempotent)."""
    ddl = open(SCHEMA).read()
    for name in sorted(CABINETS):
        with psycopg.connect(**POSTGRES) as admin:
            admin.execute(f"create schema if not exists {name}")
        with using(name), cabinet_db() as conn:
            conn.execute(ddl)
