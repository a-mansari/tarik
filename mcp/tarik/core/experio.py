"""Experio: the current cabinet's login and calls, test mode, and the guards every tool goes through."""

import contextlib
import re
import xmlrpc.client

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import log, wait
from tarik.core.cabinets import current, setting
from tarik.core.config import DOSSIER_DOMAIN, JSON_TIMEOUT, ODOO_URL, READING, RPC_TIMEOUT, TEST_MODE

# IPv4 only: the container has no IPv6 route, and httpx does not fall back when a host's IPv6 address comes first.
HTTP = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"))

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
def refused(log_text, message):
    """Whatever fails inside is logged with ``log_text`` and becomes a ToolError of ``message`` (a ToolError's own
    message is the only text that reaches the agent). "{reason}" in ``message`` is replaced by Experio's reason."""
    try:
        yield
    except ToolError:
        raise
    except Exception as error:
        log(f"{log_text}: {error}")
        raise ToolError(message.replace("{reason}", experio_refusal(error))) from None


# --- Calls ---


def rpc(path):
    """Experio's XML-RPC endpoint ``path`` (a ServerProxy), every call given up after RPC_TIMEOUT seconds."""
    base = xmlrpc.client.SafeTransport if ODOO_URL.startswith("https") else xmlrpc.client.Transport

    class Timed(base):
        def make_connection(self, host):
            connection = super().make_connection(host)
            connection.timeout = RPC_TIMEOUT
            return connection

    return xmlrpc.client.ServerProxy(f"{ODOO_URL}{path}", transport=Timed(), allow_none=True)


def login():
    """Tarik's uid in the current cabinet, checked against its ODOO_UID; PermissionError otherwise."""
    database, expected, user, key = account()
    if _logins.get((ODOO_URL, database, user, key)) == expected:
        return expected
    uid = rpc("/xmlrpc/2/common").authenticate(database, user, key, {})
    if not uid:
        raise PermissionError(f"login refused for {user} on {database}")
    if uid != expected:
        raise PermissionError(f"{user} on {database} is uid {uid}, expected {expected}: wrong cabinet or user")
    _logins[(ODOO_URL, database, user, key)] = uid
    return uid


def odoo_call(model, method, *args, **kw):
    """One authenticated XML-RPC call to the current cabinet's Experio; its answer."""
    database, _, _, key = account()
    return rpc("/xmlrpc/2/object").execute_kw(database, login(), key, model, method, list(args), kw)


def odoo_json(model, method, *args, **kw):
    """The same call over JSON-RPC, for answers holding None (onchange), which XML-RPC can't carry."""
    database, _, _, key = account()
    call = {
        "service": "object",
        "method": "execute_kw",
        "args": [database, login(), key, model, method, list(args), kw],
    }
    answer = HTTP.post(
        f"{ODOO_URL}/jsonrpc",
        timeout=JSON_TIMEOUT,
        json={"jsonrpc": "2.0", "method": "call", "params": call},
    ).json()
    if "error" in answer:
        raise RuntimeError(answer["error"].get("data", {}).get("message") or answer["error"].get("message"))
    return answer.get("result")


def in_dossier(dossier_id):
    """The context that makes Experio work in dossier ``dossier_id`` (int)."""
    return {"allowed_company_ids": [dossier_id]}


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
    dossier and exercice, not the dossier's latest one (tarik-master odoo._exercice_context)."""
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


def experio_refusal(error):
    """Experio's own message in ``error``: the last line of its traceback, 300 characters at most."""
    return str(error).strip().splitlines()[-1][:300]


def plain(html):
    """Experio's HTML message as one line of text."""
    without_tags = re.sub(r"<[^>]+>", " ", html or "")  # any HTML tag
    return re.sub(r"\s+", " ", without_tags).strip()


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
    that ends up changed, Experio's included (tarik-master odoo.fill)."""
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
