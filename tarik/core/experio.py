"""Experio: the current cabinet's login and calls, test mode, and the guards every tool goes through."""

import contextlib
import http.client
import threading
import time
import xmlrpc.client
from typing import Final

import httpx

from tarik.core.app import ToolError, log, tool, wait
from tarik.core.cabinets import current, setting
from tarik.core.config import DOSSIER_DOMAIN, JSON_TIMEOUT, ODOO_URL, READING, RPC_TIMEOUT, TEST_MODE
from tarik.core.words import strip_tags

# IPv4 only: the container has no IPv6 route, and httpx does not fall back when a host's IPv6 address comes first.
HTTP: Final = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"))
# Calls that only read: asked again once when Experio's answer is cut off or the connection drops. A write is never
# asked again: a cut answer doesn't say whether it was done.
READS = {
    "search",
    "search_read",
    "read",
    "search_count",
    "fields_get",
    "name_search",
    "read_group",
    "onchange",  # computes a form's values, saves nothing
    "parse_preview",  # reads an uploaded file
    "get_duplicated_invoice_ids",
    "get_duplicated_invoice_msg",
    "collect_validation_messages",
}


class GatewayError(Exception):
    """Experio's gateway answered 502, 503 or 504 instead of Experio."""


# An answer cut off, a connection dropped or refused, a gateway in Experio's place; never a timeout (asking again
# would only double the wait). ProtocolError: XML-RPC's 502 or 503.
CUT_OFF = (
    http.client.IncompleteRead,
    ConnectionError,
    xmlrpc.client.ProtocolError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ConnectError,
    GatewayError,
)

_logins = {}  # (server, database, user, key) -> uid: one login per cabinet and key (a new key logs in again)
_forms = {}  # cabinet -> the invoice form's fields: each database has its own


# --- The current cabinet's account and test mode ---


def expected_uid(value):
    """ODOO_UID (str) as Tarik's user id (int) in this cabinet; SystemExit unless a positive integer.
    A login returning any other id means another cabinet (or user): every call then fails."""
    if not value or not value.isdigit() or int(value) <= 0:
        raise SystemExit(f"ODOO_UID must be Tarik's user id in this cabinet, got {value!r}")
    return int(value)


def account():
    """The current cabinet's (database, Tarik's expected uid, login, key)."""
    key = setting("ODOO_API_KEY", "") or setting("ODOO_PASSWORD")
    return current(), expected_uid(setting("ODOO_UID")), setting("ODOO_USER"), key


def dry_run():
    """True (DRY_RUN=1, the default) when tools that write to Experio only say what they would write. Per cabinet."""
    return setting("DRY_RUN", "1") == "1"


def test_mode_note():
    """TEST_MODE in test mode, else "": the start of a step's message."""
    return TEST_MODE if dry_run() else ""


@contextlib.contextmanager
def refused(log_text, message, error=ToolError):
    """Whatever fails inside is logged with ``log_text`` and becomes ``error`` (a ToolError by default: its own
    message is the only text that reaches the agent) of ``message``, "{reason}" in it replaced by Experio's reason."""
    try:
        yield
    except ToolError:
        raise
    except Exception as failure:
        log(f"{log_text}: {failure}")
        raise error(message.replace("{reason}", experio_refusal(failure))) from None


# --- Calls ---


_proxies = threading.local()  # this thread's ServerProxy per (URL, path): its connection stays open between calls


def rpc(path):
    """Experio's XML-RPC endpoint ``path`` (a ServerProxy), every call given up after RPC_TIMEOUT seconds. One per
    thread and path, kept: a run makes hundreds of calls, each on the same connection instead of a new TLS one."""
    proxies = _proxies.__dict__.setdefault("by_path", {})
    if (ODOO_URL, path) not in proxies:
        base = xmlrpc.client.SafeTransport if ODOO_URL.startswith("https") else xmlrpc.client.Transport

        class Timed(base):
            def make_connection(self, host):
                connection = super().make_connection(host)
                connection.timeout = RPC_TIMEOUT
                return connection

        proxies[(ODOO_URL, path)] = xmlrpc.client.ServerProxy(f"{ODOO_URL}{path}", transport=Timed(), allow_none=True)
    return proxies[(ODOO_URL, path)]


def once_more_if_cut(read, label, call):
    """``call()``'s answer; a ``read`` cut off (CUT_OFF) is asked again once, a second later. ``label``: for the log."""
    try:
        return call()
    except CUT_OFF as error:
        if not read:
            raise
        log(f"{label} cut off ({type(error).__name__}): asked again")
        time.sleep(1)
        return call()


def login():
    """Tarik's uid in the current cabinet, checked against its ODOO_UID; PermissionError otherwise."""
    database, expected, user, key = account()
    if _logins.get((ODOO_URL, database, user, key)) == expected:
        return expected
    uid = once_more_if_cut(
        True, "login", lambda: rpc("/xmlrpc/2/common").authenticate(database, user, key, {})
    )  # checks the key, writes nothing
    if not uid:
        raise PermissionError(f"login refused for {user} on {database}")
    if uid != expected:
        raise PermissionError(f"{user} on {database} is uid {uid}, expected {expected}: wrong cabinet or user")
    _logins[(ODOO_URL, database, user, key)] = uid
    return uid


def odoo_call(model, method, *args, **kw):
    """One authenticated XML-RPC call to the current cabinet's Experio; its answer. A read (READS) cut off is asked
    again once."""
    database, _, _, key = account()
    return once_more_if_cut(
        method in READS,
        f"odoo_call {model}.{method}",
        lambda: rpc("/xmlrpc/2/object").execute_kw(database, login(), key, model, method, list(args), kw),
    )


def odoo_json(model, method, *args, **kw):
    """The same call over JSON-RPC, for answers holding None (onchange), which XML-RPC can't carry. A read (READS)
    cut off is asked again once."""
    database, _, _, key = account()
    call = {
        "service": "object",
        "method": "execute_kw",
        "args": [database, login(), key, model, method, list(args), kw],
    }

    def post():
        response = HTTP.post(
            f"{ODOO_URL}/jsonrpc", timeout=JSON_TIMEOUT, json={"jsonrpc": "2.0", "method": "call", "params": call}
        )
        if response.status_code in (502, 503, 504):
            raise GatewayError(response.status_code)
        return response.json()

    answer = once_more_if_cut(method in READS, f"odoo_json {model}.{method}", post)
    if "error" in answer:
        raise RuntimeError(answer["error"].get("data", {}).get("message") or answer["error"].get("message"))
    return answer.get("result")


def in_dossier(dossier_id):
    """The context that makes Experio work in dossier ``dossier_id`` (int)."""
    return {"allowed_company_ids": [dossier_id]}


def main_dossier_context():
    """The context that makes Experio create a dossier as if from the cabinet's main one (base.main_company): a new
    dossier takes its defaults (journals, accounts, taxes) from the creator's current dossier, which is a client's,
    whose ones the new dossier then cannot read. The main one has none, so the new dossier starts empty and Experio
    fills its settings from the dossier's own journals and accounts as they are created or imported."""
    main = odoo_call(
        "ir.model.data", "search_read", [("module", "=", "base"), ("name", "=", "main_company")], fields=["res_id"]
    )
    return in_dossier(main[0]["res_id"])


# --- Guards: a real dossier or invoice, else a ToolError the agent reads ---


def _require_id(value, message):
    """ToolError(``message``) unless ``value`` is a positive int (a bool is not)."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ToolError(message)


def require_dossier(dossier_id):
    """The dossier's name (str). Every tool working in a dossier calls this first: without a real
    dossier id from find_dossier, it fails instead of guessing."""
    _require_id(dossier_id, "dossier_id manquant ou invalide : trouvez d'abord le dossier avec Experio_find_dossier.")
    with refused(f"require_dossier {dossier_id}", "Experio est injoignable : le dossier n'a pas pu etre verifie."):
        found = odoo_call("res.company", "search_read", [("id", "=", dossier_id), *DOSSIER_DOMAIN], fields=["name"])
    if not found:
        raise ToolError(f"Aucun dossier n° {dossier_id} : trouvez-le avec Experio_find_dossier.")
    return found[0]["name"]


def require_invoice(invoice_id, fields):
    """The invoice's ``fields`` (dict). Every tool working on an invoice calls this first: it fails
    unless the id is a real invoice of one of the cabinet's dossiers."""
    _require_id(
        invoice_id, "invoice_id manquant ou invalide : c'est le numero de facture brouillon donne a sa creation."
    )
    in_a_dossier = [(f"company_id.{field}", op, value) for field, op, value in DOSSIER_DOMAIN]
    with refused(f"require_invoice {invoice_id}", "Experio est injoignable : la facture n'a pas pu etre lue."):
        found = odoo_call("exp.invoice", "search_read", [("id", "=", invoice_id), *in_a_dossier], fields=fields)
    if not found:
        raise ToolError(f"Aucune facture n° {invoice_id} dans les dossiers du cabinet.")
    return found[0]


def require_draft(invoice_id, invoice, what="etre modifiee"):
    """ToolError unless ``invoice`` (with its state) is a draft: only a draft may ``what``."""
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut {what}.")


def require_draft_invoice(invoice_id, fields, what="etre modifiee", read=False):
    """The invoice's ``fields`` (with its state), ToolError unless it is a draft that may ``what``; with ``read``,
    unless Experio has finished reading it too (``fields`` then holds bluejay_state)."""
    invoice = require_invoice(invoice_id, fields)
    require_draft(invoice_id, invoice, what)
    if read and (waiting := still_reading(invoice)):
        raise ToolError(waiting["message"])
    return invoice


def still_reading(invoice):
    """A wait result while Experio still reads the scan of ``invoice`` (with id and bluejay_state), else None."""
    if invoice["bluejay_state"] in READING:
        return wait(f"Facture {invoice['id']} : Experio est encore en train de la lire, reessayez dans une minute.")
    return None


def has_scan(invoice):
    """True when ``invoice`` (with document_pdf and file_type) has a PDF scan to read."""
    return bool(invoice["document_pdf"]) and invoice["file_type"] in (False, None, "pdf")


# --- Invoices ---


def invoice_context(invoice):
    """The context that makes Experio validate ``invoice`` (with company_id and exercice_id) against its own
    dossier and exercice, not the dossier's latest one."""
    context = in_dossier(invoice["company_id"][0])
    if invoice["exercice_id"]:
        context["cron_fiscal_entry_id"] = invoice["exercice_id"][0]
    return context


def account_code(invoice):
    """The code of ``invoice``'s account ("6136" of "6136 Honoraires"), "" without one."""
    return (invoice["account_id"] or [0, ""])[1].split(" ")[0]


def refresh(invoice_id, context):
    """Experio's own "Actualiser" on the invoice (it rebuilds the lines), skipped in test mode.
    It returns None, hence JSON-RPC."""
    if not dry_run():
        odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)


def misplaced_journals(company_id, fields):
    """{setting: journal name, or None when unset} among the dossier's journal settings ``fields`` that are unset or
    point to another dossier's journal: Experio gives every new dossier its first dossier's journals as defaults,
    which the dossier's own accounting then cannot read ("enregistrements top secrets")."""
    company = odoo_call("res.company", "read", [company_id], fields=list(fields))[0]
    ids = [company[field][0] for field in fields if company[field]]
    owners = {}
    if ids:
        found = odoo_call("exp.journal", "search_read", [("id", "in", ids)], fields=["company_id"])
        owners = {journal["id"]: journal["company_id"][0] for journal in found}
    return {
        field: company[field] and company[field][1]
        for field in fields
        if not company[field] or owners.get(company[field][0]) != company_id
    }


def blocking(invoice_id, context):
    """What Experio's own check (the Comptabiliser button's) says blocks posting the invoice ([str], plain text).
    Its warnings too: Tarik posts only when the check says nothing at all."""
    messages = odoo_json("exp.invoice", "collect_validation_messages", [invoice_id], context=context) or []
    return [plain(text) for _level, text in messages]


def experio_refusal(error):
    """Experio's own message in ``error``: the last line of its traceback, 300 characters at most."""
    return str(error).strip().splitlines()[-1][:300]


def plain(html):
    """Experio's HTML message as one line of text."""
    return " ".join(strip_tags(html, " ").split())


def experio_notes(invoice_id, context):
    """Experio's own error and warning on the invoice, as " Erreur Experio : ... Avertissement Experio : ..."
    to end a message with, "" when there is none."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["error_msg", "warning_msg"], context=context)[0]
    labelled = (("Erreur Experio", invoice["error_msg"]), ("Avertissement Experio", invoice["warning_msg"]))
    return "".join(f" {label} : {plain(html)}" for label, html in labelled if plain(html))


FORM_SKIP = {"one2many", "many2many", "binary", "html", "properties", "json"}  # field types the form can't send back
TECHNICAL = {"id", "create_uid", "write_uid", "create_date", "write_date"}


def _form():
    """The current cabinet's invoice form: {"types": {field: type}, "editable": {field}}, read once."""
    form = _forms.setdefault(current(), {})
    if not form:
        meta = odoo_call("exp.invoice", "fields_get", [], attributes=["type", "store", "readonly"])
        form["types"] = {
            name: field["type"]
            for name, field in meta.items()
            if field.get("store")
            and field["type"] not in FORM_SKIP
            and not name.startswith("__")
            and name not in TECHNICAL
        }
        form["editable"] = {name for name, field in meta.items() if not field.get("readonly")}
    return form


def fill_invoice(invoice_id, changes, context, save=True):
    """Type ``changes`` ({field: value}) into the invoice form one field at a time, as a user does: after each
    one Experio's onchange fills what it fills on screen (exchange rate, withholding...). Then save, unless
    DRY_RUN or ``save`` is False (only to see what Experio would fill). Returns {field: value} of every field
    that ends up changed, Experio's included."""
    form = _form()
    types = form["types"]
    record = odoo_call("exp.invoice", "read", [invoice_id], fields=list(types), context=context)[0]
    # A many2one reads as [id, name] and is written as its id.
    values = {
        name: (value[0] if types[name] == "many2one" and value else value)
        for name, value in record.items()
        if name in types
    }
    start = dict(values)
    spec = {name: {} for name in types}
    for name, value in changes.items():
        values[name] = value
        answer = odoo_json("exp.invoice", "onchange", [invoice_id], values, [name], spec, context=context)
        for key, new in ((answer or {}).get("value") or {}).items():
            if key in types:
                values[key] = new.get("id") if isinstance(new, dict) else new
    changed = {
        name: value
        for name, value in values.items()
        if value != start.get(name) and (name in form["editable"] or name in changes)
    }
    if changed and save and not dry_run():
        odoo_call("exp.invoice", "write", [invoice_id], changed, context=context)
    return changed


# --- Can Tarik reach Experio? ---


@tool
def experio_connection() -> str:
    """Check that Tarik can reach the Experio system and log in. Call it when
    asked whether Experio works, or before anything else when in doubt."""
    # Connection details, and the fact that Experio runs on Odoo, stay in this log: the agent never sees them.
    try:
        version = rpc("/xmlrpc/2/common").version()["server_version"]
    except Exception as error:
        log(f"experio_connection: {ODOO_URL} unreachable: {error}")
        return "Experio est injoignable pour le moment."
    try:
        # A real read, not only the login: a password can log in yet be refused every call.
        odoo_call("res.company", "search_count", [])
    except Exception as error:
        database, _, user, _ = account()
        log(f"experio_connection: {user} on {database} refused: {error}")
        return "Experio repond, mais ma connexion y est refusee."
    log(f"experio_connection: ok, Odoo {version}")
    return "Connecte au systeme Experio."
