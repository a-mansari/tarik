"""Experio calls for the current cabinet: login, XML-RPC and JSON-RPC calls, test mode (DRY_RUN), the guards every
tool goes through, and the invoice form helpers."""

import contextlib
import http.client
import threading
import time
import xmlrpc.client
from typing import Final

import httpx

from engine.tools.core.app import ToolError, log, tool, wait
from engine.tools.core.cabinets import current, setting
from engine.tools.core.config import DOSSIER_DOMAIN, JSON_TIMEOUT, ODOO_URL, READING, RPC_TIMEOUT, TEST_MODE
from engine.tools.core.words import strip_tags

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


class ExperioRefusal(RuntimeError):
    """Experio answered a JSON-RPC call with an error. Its message is Experio's; ``name`` is the exception's class
    ("odoo.exceptions.ValidationError"), "" when Experio gives none."""

    def __init__(self, message, name=""):
        super().__init__(message)
        self.name = name or ""


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
    """Return ODOO_UID ``value`` (str) as Tarik's user id (int) in this cabinet. Raises SystemExit unless it is a
    positive integer. A login returning any other id means another cabinet (or user): every call then fails."""
    if not value or not value.isdigit() or int(value) <= 0:
        raise SystemExit(f"ODOO_UID must be Tarik's user id in this cabinet, got {value!r}")
    return int(value)


def account():
    """Return the current cabinet's (database, Tarik's expected uid, login, key): the key is ODOO_API_KEY, else
    ODOO_PASSWORD."""
    key = setting("ODOO_API_KEY", "") or setting("ODOO_PASSWORD")
    return current(), expected_uid(setting("ODOO_UID")), setting("ODOO_USER"), key


def dry_run():
    """True when the current cabinet is in test mode (DRY_RUN "1", the default): tools that write to Experio only
    report what they would write."""
    return setting("DRY_RUN", "1") == "1"


def test_mode_note():
    """Return TEST_MODE in test mode, else "": the start of a step result's message."""
    return TEST_MODE if dry_run() else ""


def would(done):
    """Return ``done`` (« créé »), or « serait créé » in test mode, where nothing is written."""
    return f"serait {done}" if dry_run() else done


_DOSSIER_LOCKS = {}  # (cabinet, dossier id) -> its lock
_LOCKS_LOCK = threading.Lock()


def dossier_lock(dossier_id):
    """Return the lock of dossier ``dossier_id`` in this cabinet: held around a check-then-make of the dossier's own
    setup (a journal, its account), which the files of one message run in parallel would otherwise each make.
    shortcut: one server process only (as engine/live.py); several would need a Postgres advisory lock."""
    with _LOCKS_LOCK:
        return _DOSSIER_LOCKS.setdefault((current(), dossier_id), threading.Lock())


@contextlib.contextmanager
def refused(log_text, message, error=ToolError):
    """Context manager: turn any exception raised in the block into ``error`` (ToolError by default) with ``message``.

    A ToolError passes through unchanged. Any other exception is logged with ``log_text``, then re-raised as
    ``error(message)``, "{reason}" in ``message`` replaced by Experio's reason (experio_refusal). Only a ToolError's
    own message reaches the agent, so the original exception stays in the log."""
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
    """Return a ServerProxy for Experio's XML-RPC endpoint ``path``, each call given up after RPC_TIMEOUT seconds.
    One is kept per thread and path: a run makes hundreds of calls, all on one connection instead of a new TLS one
    each."""
    proxies = _proxies.__dict__.setdefault("by_path", {})
    endpoint = (ODOO_URL, path)
    if endpoint not in proxies:
        transport = _timed_transport()
        proxies[endpoint] = xmlrpc.client.ServerProxy(f"{ODOO_URL}{path}", transport=transport, allow_none=True)
    return proxies[endpoint]


def _timed_transport():
    """Return an XML-RPC transport for ODOO_URL (HTTPS or not) whose connections give up after RPC_TIMEOUT seconds."""
    base = xmlrpc.client.SafeTransport if ODOO_URL.startswith("https") else xmlrpc.client.Transport

    class Timed(base):
        def make_connection(self, host):
            connection = super().make_connection(host)
            connection.timeout = RPC_TIMEOUT
            return connection

    return Timed()


def once_more_if_cut(read, label, call):
    """Return ``call()``'s answer. When ``read`` is True and the answer is cut off (CUT_OFF), ``call`` is made again
    once, a second later; a write's cut-off error is raised. ``label`` names the call in the log."""
    try:
        return call()
    except CUT_OFF as error:
        if not read:
            raise
        log(f"{label} cut off ({type(error).__name__}): asked again")
        time.sleep(1)
        return call()


def login():
    """Return Tarik's uid in the current cabinet, logging in once per account. Raises PermissionError when the login
    is refused or the uid is not the cabinet's ODOO_UID."""
    database, expected, user, key = account()
    if _logins.get((ODOO_URL, database, user, key)) == expected:
        return expected
    # Checks the key, writes nothing: a read.
    uid = once_more_if_cut(True, "login", lambda: rpc("/xmlrpc/2/common").authenticate(database, user, key, {}))
    if not uid:
        raise PermissionError(f"login refused for {user} on {database}")
    if uid != expected:
        raise PermissionError(f"{user} on {database} is uid {uid}, expected {expected}: wrong cabinet or user")
    _logins[(ODOO_URL, database, user, key)] = uid
    return uid


def odoo_call(model, method, *args, **kw):
    """Call ``model``.``method`` with ``args`` and keyword ``kw`` over XML-RPC in the current cabinet's Experio, and
    return the answer. A read (READS) that is cut off is made again once."""
    database, _, _, key = account()
    return once_more_if_cut(
        method in READS,
        f"odoo_call {model}.{method}",
        lambda: rpc("/xmlrpc/2/object").execute_kw(database, login(), key, model, method, list(args), kw),
    )


def odoo_json(model, method, *args, **kw):
    """Call ``model``.``method`` like odoo_call, over JSON-RPC, for answers holding None (onchange, action_refresh),
    which XML-RPC can't carry. A read (READS) that is cut off is made again once. Returns the call's result.
    Raises ExperioRefusal when Experio answers with an error, GatewayError on a 502, 503 or 504."""
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
        data = answer["error"].get("data", {})
        raise ExperioRefusal(data.get("message") or answer["error"].get("message"), data.get("name"))
    return answer.get("result")


def in_dossier(dossier_id):
    """Return the context that makes Experio work in dossier ``dossier_id`` (int) only."""
    return {"allowed_company_ids": [dossier_id]}


def main_company_id():
    """Return the id of the cabinet's own company in Experio (xmlid base.main_company, which Experio uses for the
    cabinet: tax_calendar reminder_scheduler.py, « #NOM_CABINET »)."""
    main = odoo_call(
        "ir.model.data", "search_read", [("module", "=", "base"), ("name", "=", "main_company")], fields=["res_id"]
    )
    return main[0]["res_id"]


def main_dossier_context():
    """Return the context that makes Experio create a dossier from the cabinet's main company (base.main_company).

    A new dossier takes its default journals and accounts from the creator's current company
    (res.company _default_*: self.env.company). If that company is a client dossier, the new dossier's settings
    point to that client's journals and accounts, which the new dossier cannot read (record rule
    exp_journal_company_rule). The main company has none, so the new dossier's settings start empty, and Experio
    fills them from the dossier's own journals and accounts as they are created (auto_detect_settings)."""
    return in_dossier(main_company_id())


# --- Guards: a real dossier or invoice, else a ToolError the agent reads ---


def _require_id(value, message):
    """Raise ToolError(``message``) unless ``value`` is a positive int (a bool is not)."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ToolError(message)


def require_dossier(dossier_id):
    """Return the name (str) of dossier ``dossier_id``. Every tool working in a dossier calls this first.
    Raises ToolError when the id is invalid, is not one of the cabinet's dossiers (DOSSIER_DOMAIN), or Experio
    can't be reached: without a real dossier id from find_dossier, the tool fails instead of guessing."""
    _require_id(dossier_id, "dossier_id manquant ou invalide : trouvez d'abord le dossier avec Experio_find_dossier.")
    with refused(f"require_dossier {dossier_id}", "Experio est injoignable : le dossier n'a pas pu etre verifie."):
        found = odoo_call("res.company", "search_read", [("id", "=", dossier_id), *DOSSIER_DOMAIN], fields=["name"])
    if not found:
        raise ToolError(f"Aucun dossier n° {dossier_id} : trouvez-le avec Experio_find_dossier.")
    return found[0]["name"]


def require_invoice(invoice_id, fields):
    """Return invoice ``invoice_id``'s ``fields`` (dict, with its id). Every tool working on an invoice calls this
    first. Raises ToolError unless the id is a real invoice of one of the cabinet's dossiers, or when Experio
    can't be reached."""
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
    """Raise ToolError unless ``invoice`` (dict with "state") is a draft. ``what``: the French action refused,
    as in « seule une facture brouillon peut {what} »."""
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut {what}.")


def require_draft_invoice(invoice_id, fields, what="etre modifiee", read=False):
    """Return invoice ``invoice_id``'s ``fields`` (dict; ``fields`` must include state). Raises ToolError unless it
    is a draft (``what``: as in require_draft); with ``read``, also while Experio is still reading its scan
    (``fields`` must then include bluejay_state)."""
    invoice = require_invoice(invoice_id, fields)
    require_draft(invoice_id, invoice, what)
    if not read:
        return invoice
    waiting = still_reading(invoice)
    if waiting:
        raise ToolError(waiting["message"])
    return invoice


def still_reading(invoice):
    """Return a wait result while Experio still reads the scan of ``invoice`` (dict with id and bluejay_state),
    else None."""
    if invoice["bluejay_state"] in READING:
        return wait(f"Facture {invoice['id']} : Experio est encore en train de la lire, reessayez dans une minute.")
    return None


def has_scan(invoice):
    """True when ``invoice`` (with document_pdf and file_type) has a PDF scan to read: any document but an Excel one.
    file_type stays "none" (its default) on PDFs Experio stores itself: the drafts split off one scan
    (exp_bluejay_job.py document_values) and Facturation's (gst_invoice.py)."""
    return bool(invoice["document_pdf"]) and invoice["file_type"] != "excel"


# --- Invoices ---


def invoice_context(invoice):
    """Return the context that makes Experio work on ``invoice`` (dict with company_id and exercice_id) in its own
    dossier and exercice. Without cron_fiscal_entry_id, Experio's exp.fiscal.entries get_selected_fiscal_entry
    falls back to the dossier's latest exercice."""
    context = in_dossier(invoice["company_id"][0])
    if invoice["exercice_id"]:
        context["cron_fiscal_entry_id"] = invoice["exercice_id"][0]
    return context


def account_code(invoice):
    """Return the code of ``invoice``'s account ("6136" of "6136 Honoraires"); "" without one."""
    if not invoice["account_id"]:
        return ""
    name = invoice["account_id"][1]  # [id, "6136 Honoraires"]
    return name.split(" ")[0]


def refresh(invoice_id, context):
    """Run Experio's own « Actualiser » on invoice ``invoice_id`` (exp.invoice action_refresh: rebuilds its TVA and
    accounting lines); nothing in test mode. action_refresh returns None, hence JSON-RPC."""
    if not dry_run():
        odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)


def misplaced_journals(company_id, fields):
    """Return the journal settings among ``fields`` (res.company fields) of dossier ``company_id`` that are unset or
    point to another dossier's journal, as {field: journal name, or False when unset}; {} when all are right.

    Experio defaults a new dossier's journals to those of the creating user's current company
    (res.company _default_purchase_journal_id: self.env.company), and inside the dossier Experio then refuses to
    read another dossier's journal (record rule exp_journal_company_rule: « enregistrements top secrets »)."""
    company = odoo_call("res.company", "read", [company_id], fields=list(fields))[0]
    ids = [company[field][0] for field in fields if company[field]]
    owners = {}
    if ids:
        found = odoo_call("exp.journal", "search_read", [("id", "in", ids)], fields=["company_id"])
        owners = {journal["id"]: journal["company_id"][0] for journal in found}
    misplaced = {}
    for field in fields:
        journal = company[field]  # [id, name], or False when unset
        if not journal:
            misplaced[field] = journal
        elif owners.get(journal[0]) != company_id:
            misplaced[field] = journal[1]
    return misplaced


def checked(invoice_id, context):
    """Return Experio's own check of invoice ``invoice_id`` (exp.invoice collect_validation_messages, the one
    « Comptabiliser » runs first) as [(level, plain text)]. Levels: "cgnc" for the dossier's settings, "error" for
    the invoice's own fields and amounts, "warning" for warnings not dismissed on the invoice."""
    messages = odoo_json("exp.invoice", "collect_validation_messages", [invoice_id], context=context) or []
    return [(level, plain(text)) for level, text in messages]


def blocking(invoice_id, context):
    """Return what Experio's own check says blocks posting invoice ``invoice_id`` ([str], plain text): every "cgnc"
    or "error" message. Warnings are left out: action_post raises only when "cgnc" or "error" messages exist;
    settle_warnings handles warnings."""
    return [text for level, text in checked(invoice_id, context) if level != "warning"]


# Experio's own refusals: a check that warns raises ValidationError, one that stops raises UserError.
CHECK_REFUSALS = ("odoo.exceptions.ValidationError", "odoo.exceptions.UserError")


def warns(invoice_id, check, context):
    """Run Experio's warning check ``check`` (an exp.invoice validate_warning_... method) on invoice ``invoice_id``.
    Returns its message as plain text, or None when it raises nothing or crashes.

    Called alone over RPC, its ValidationError rolls back the warning_code it writes before raising; inside
    collect_validation_messages the error is caught, so that write is kept."""
    try:
        odoo_json("exp.invoice", check, [invoice_id], context=context)
    except ExperioRefusal as error:
        if error.name not in CHECK_REFUSALS:
            # A check crashing (validate_warning_account_id on an invoice without account): Experio's own list of
            # warnings skips it the same way (collect_validation_messages: except Exception: pass).
            log(f"warns {invoice_id} {check}: Experio crashed ({error.name}: {error})")
            return None
        return plain(str(error))
    return None


def experio_refusal(error):
    """Return Experio's own message in ``error`` on one line, 300 characters at most: an ExperioRefusal's whole
    message (it may hold several lines: « ...for the company: \n: DOSSIER »), else the last line of the error's
    text (an XML-RPC fault's traceback)."""
    if isinstance(error, ExperioRefusal):
        return " ".join(str(error).split())[:300]
    return str(error).strip().splitlines()[-1][:300]


def plain(html):
    """Return Experio's HTML message ``html`` as one line of plain text."""
    return " ".join(strip_tags(html, " ").split())


FORM_SKIP = {"one2many", "many2many", "binary", "html", "properties", "json"}  # field types the form can't send back
TECHNICAL = {"id", "create_uid", "write_uid", "create_date", "write_date"}


def _form():
    """Return the current cabinet's exp.invoice form, read once per cabinet: {"types": {field: type} of the fields
    the form sends back, "editable": {fields not readonly}}."""
    form = _forms.setdefault(current(), {})
    if not form:
        meta = odoo_call("exp.invoice", "fields_get", [], attributes=["type", "store", "readonly"])
        form["types"] = {name: field["type"] for name, field in meta.items() if _sent_back(name, field)}
        form["editable"] = {name for name, field in meta.items() if not field.get("readonly")}
    return form


def _sent_back(name, field):
    """True when the form sends field ``name`` (its fields_get ``field``) back: stored, of a type it can send, and
    neither technical nor internal (__...)."""
    if not field.get("store") or field["type"] in FORM_SKIP:
        return False
    return not name.startswith("__") and name not in TECHNICAL


def _form_values(invoice_id, types, context):
    """Return invoice ``invoice_id``'s form values ({field: value}) for the fields in ``types``, as the form sends
    them back (a many2one as its id)."""
    record = odoo_call("exp.invoice", "read", [invoice_id], fields=list(types), context=context)[0]
    values = {}
    for name, value in record.items():
        if name not in types:
            continue
        # A many2one reads as [id, name] and is written as its id.
        values[name] = value[0] if types[name] == "many2one" and value else value
    return values


def _type_in(invoice_id, values, name, types, context):
    """Update ``values`` (the form's {field: value}, field ``name`` just typed in) in place with what Experio's
    onchange fills in after it, limited to the fields in ``types``."""
    spec = {field: {} for field in types}
    answer = odoo_json("exp.invoice", "onchange", [invoice_id], values, [name], spec, context=context)
    filled = (answer or {}).get("value") or {}
    for key, new in filled.items():
        if key in types:
            values[key] = new.get("id") if isinstance(new, dict) else new


def fill_invoice(invoice_id, changes, context, save=True):
    """Type ``changes`` ({field: value}) into invoice ``invoice_id``'s form one field at a time, as a user does:
    after each one, Experio's onchange fills in what it fills on screen (exchange rate, withholding...). Then save,
    except in test mode or when ``save`` is False (to see what Experio would fill).

    Returns {field: value} of every field that ends up changed, those Experio filled included. Raises ToolError
    when saving would replace or clear a tiers (partner_id) already set: only the cabinet's staff change it."""
    form = _form()
    types = form["types"]
    values = _form_values(invoice_id, types, context)
    start = dict(values)
    for name, value in changes.items():
        values[name] = value
        _type_in(invoice_id, values, name, types, context)

    changed = {
        name: value
        for name, value in values.items()
        if value != start.get(name) and (name in form["editable"] or name in changes)
    }
    # A supplier or customer already set in Experio is never replaced nor cleared by Tarik, whatever the scan or an
    # onchange says: only the cabinet's staff change it, in Experio.
    if save and start.get("partner_id") and values.get("partner_id") != start["partner_id"]:
        log(f"fill_invoice {invoice_id}: partner {start['partner_id']} -> {values.get('partner_id')} refused")
        raise ToolError(
            f"Facture {invoice_id} : son tiers est deja choisi dans Experio ; Tarik ne le remplace pas. "
            "Un collaborateur du cabinet peut le changer dans Experio s'il n'est pas le bon."
        )
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
