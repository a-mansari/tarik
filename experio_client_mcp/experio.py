"""Reading Experio as the client: every call carries the client's own login and key, so Experio decides what that user
may read (its record rules and groups); this server holds no credential of its own. Only reads can be made: the
allowed methods are fixed below, no tool names a method."""

import contextvars
import http.client
import logging
import threading
import xmlrpc.client
from dataclasses import dataclass
from typing import Final

from experio_client_mcp import audit, config

READS: Final = {"search", "search_read", "search_count", "read", "read_group", "fields_get"}
# Experio's client dossiers (engine/tools/core/config.py DOSSIER_DOMAIN in Tarik): enabled, not the cabinet's own
# company (id 1), not deleted.
DOSSIERS: Final = (("enabled", "=", True), ("id", "!=", 1), ("is_deleted", "=", False))
# Experio's read_group alone keeps only the selected exercice's posted invoices and bank operations
# (exp_account/models/base.py:575-592, :332-357): a question is about any year, so the filter is skipped.
SKIP_EXERCICE: Final = {"skip_fiscal_year_filter": True}

log = logging.getLogger("experio_client_mcp")


class Refusal(Exception):
    """A refusal the client may read: its message names no internal (no model, no SQL, no trace, no other dossier)."""


@dataclass(frozen=True)
class Session:
    """One authenticated request: the cabinet (its Experio database), its address, the login, the key and its user
    id."""

    cabinet: str
    url: str
    login: str
    key: str
    uid: int


SESSION: contextvars.ContextVar[Session] = contextvars.ContextVar("session")
_local = threading.local()  # this thread's proxies: one connection per thread and endpoint


class _Transport(xmlrpc.client.SafeTransport):
    """HTTPS XML-RPC with a timeout; the certificate and the host name are checked, as by default."""

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = config.TIMEOUT
        return connection


class _PlainTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = config.TIMEOUT
        return connection


def _proxy(url, path):
    proxies = _local.__dict__.setdefault("proxies", {})
    if (url, path) not in proxies:
        transport = _PlainTransport() if url.startswith("http://") else _Transport()
        proxies[url, path] = xmlrpc.client.ServerProxy(f"{url}{path}", transport=transport, allow_none=True)
    return proxies[url, path]


class Unreachable(Exception):
    """Experio did not answer (the network, a gateway, a timeout): not a wrong credential."""


def authenticate(cabinet, url, login, key):
    """Return the Experio user id of ``login`` in database ``cabinet`` with ``key`` (a password or an API key), 0 when
    Experio refuses them. Raises Unreachable when Experio does not answer."""
    try:
        return int(_proxy(url, "/xmlrpc/2/common").authenticate(cabinet, login, key, {}) or 0)
    except xmlrpc.client.Fault:
        return 0  # an unknown database or login: the same answer as a wrong key, so nothing is told apart
    except (OSError, http.client.HTTPException, xmlrpc.client.ProtocolError) as error:
        log.warning("experio unreachable: %s", type(error).__name__)
        raise Unreachable from None


def in_dossier(dossier_id):
    """Return the context that makes Experio work in dossier ``dossier_id`` only (and any year: SKIP_EXERCICE)."""
    return {"allowed_company_ids": [dossier_id], **SKIP_EXERCICE}


def odoo_call(model, method, *args, **kw):
    """Call ``model``.``method`` as this request's user and return the answer. ``kw["context"]`` is merged over
    SKIP_EXERCICE. Raises ValueError for a method that is not a read."""
    if method not in READS:
        raise ValueError(f"{method} is not a read")
    session = SESSION.get()
    kw["context"] = {**SKIP_EXERCICE, **(kw.get("context") or {})}
    return _proxy(session.url, "/xmlrpc/2/object").execute_kw(
        session.cabinet, session.uid, session.key, model, method, list(args), kw
    )


class experio_errors:  # noqa: N801 -- a context manager used like a statement
    """Context manager: turn what goes wrong in an Experio call into a Refusal the client may read. Experio's own
    message is never passed on (it can carry a trace, a model, a path): it goes to the log, by class only."""

    def __init__(self, what="la lecture"):
        self.what = what

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        if error is None or isinstance(error, Refusal):
            return False
        audit.event("experio_error", what=self.what, error=kind.__name__)
        if isinstance(error, xmlrpc.client.Fault) and error.faultCode in (2, 3, 4):  # access refused by Experio
            raise Refusal(f"Experio refuse l'accès à {self.what} pour ce compte.") from None
        if isinstance(error, xmlrpc.client.Fault | xmlrpc.client.ProtocolError | OSError | http.client.HTTPException):
            raise Refusal(f"Experio n'a pas pu faire {self.what} : réessayez plus tard.") from None
        return False


def an_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


NOT_YOURS = "Dossier inconnu ou non accessible avec ce compte."  # the same words for a dossier that does not exist


def require_dossier(dossier_id):
    """Return the name of dossier ``dossier_id`` (an id). Every tool working in a dossier calls this first. Raises
    Refusal, with the same words, when the id is invalid, is not a client dossier, or is not one of the companies this
    user may open: a client learns nothing about a dossier that is not theirs."""
    if not an_id(dossier_id):
        raise Refusal("dossier_id : un numéro de dossier (list_dossiers) est attendu.")
    with experio_errors("la lecture du dossier"):
        session = SESSION.get()
        mine = odoo_call("res.users", "read", [session.uid], fields=["company_ids"])[0]["company_ids"]
        found = odoo_call("res.company", "search_read", [("id", "=", dossier_id), *DOSSIERS], fields=["name"])
    if dossier_id not in mine or not found:
        raise Refusal(NOT_YOURS)
    return found[0]["name"]
