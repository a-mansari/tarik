"""Experio: the current cabinet's login and calls, the cabinet lock, and the guards every tool goes through."""

import os
import re
import xmlrpc.client

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.cabinets import current, setting


ODOO_URL = os.environ["ODOO_URL"].rstrip("/")  # the Experio server, the same for every cabinet

def exact_cabinet(name):
    """The cabinet's database name, taken exactly as configured. No default, no
    trimming, no case change: a name that isn't exact must stop Tarik, never
    land it in another cabinet."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise SystemExit(f"ODOO_DB must be the cabinet's exact database name, got {name!r}")
    return name

def expected_uid(value):
    """The user id Tarik has in this cabinet. A login returning any other id
    means another cabinet (or user): every call then fails."""
    if not value or not value.isdigit() or int(value) <= 0:
        raise SystemExit(f"ODOO_UID must be Tarik's user id in this cabinet, got {value!r}")
    return int(value)

def account():
    """The current cabinet's Experio database, Tarik's user id there, login and key."""
    return (exact_cabinet(current()), expected_uid(setting("ODOO_UID")), setting("ODOO_USER"),
            setting("ODOO_API_KEY", "") or setting("ODOO_PASSWORD"))

def dry_run():
    """1 (the default): tools that write to Experio only say what they would write. Per cabinet."""
    return setting("DRY_RUN", "1") == "1"

# IPv4 only: the container has no IPv6 route, and httpx does not fall back when a host's IPv6 address comes first.
HTTP = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"))

def login():
    """Tarik's uid in the current cabinet, checked against its ODOO_UID."""
    database, expected, user, key = account()
    uid = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common").authenticate(database, user, key, {})
    if not uid:
        raise PermissionError(f"login refused for {user} on {database}")
    if uid != expected:
        raise PermissionError(f"{user} on {database} is uid {uid}, expected {expected}: wrong cabinet or user")
    return uid

def odoo_call(model, method, *args, **kw):
    """One authenticated call to the current cabinet's Experio."""
    database, _, _, key = account()
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return models.execute_kw(database, login(), key, model, method, list(args), kw)

def odoo_json(model, method, *args, **kw):
    """The same call over JSON-RPC, for answers holding None (onchange), which XML-RPC can't carry."""
    answer = HTTP.post(f"{ODOO_URL}/jsonrpc", timeout=600, json={"jsonrpc": "2.0", "method": "call", "params": {
        "service": "object", "method": "execute_kw",
        "args": [account()[0], login(), account()[3], model, method, list(args), kw]}}).json()
    if "error" in answer:
        raise RuntimeError(answer["error"].get("data", {}).get("message") or answer["error"].get("message"))
    return answer.get("result")

# Experio's own definition of a client dossier (tax calendar, notifications, bank...):
# enabled, not company 1 (the cabinet itself), not deleted. Archived ones are inactive, hence hidden.
DOSSIER_DOMAIN = [("enabled", "=", True), ("id", "!=", 1), ("is_deleted", "=", False)]

def require_dossier(dossier_id):
    """The dossier's name. Every tool working in a dossier calls this first:
    without a real dossier id from find_dossier, it fails instead of guessing.
    ToolError, not ValueError: only a ToolError's message reaches the agent."""
    if not isinstance(dossier_id, int) or isinstance(dossier_id, bool) or dossier_id <= 0:
        raise ToolError("dossier_id manquant ou invalide : trouvez d'abord le dossier avec find_dossier.")
    try:
        found = odoo_call("res.company", "search_read", [("id", "=", dossier_id), *DOSSIER_DOMAIN], fields=["name"])
    except Exception as error:
        print(f"require_dossier {dossier_id}: {error}", flush=True)
        raise ToolError("Experio est injoignable : le dossier n'a pas pu etre verifie.") from None
    if not found:
        raise ToolError(f"Aucun dossier n° {dossier_id} : trouvez-le avec find_dossier.")
    return found[0]["name"]

def require_invoice(invoice_id, fields):
    """The invoice's fields. Every tool working on an invoice calls this first: it fails
    unless the id is a real invoice of one of the cabinet's dossiers."""
    if not isinstance(invoice_id, int) or isinstance(invoice_id, bool) or invoice_id <= 0:
        raise ToolError("invoice_id manquant ou invalide : c'est le numero de facture brouillon donne a sa creation.")
    domain = [("id", "=", invoice_id), *[(f"company_id.{field}", op, value) for field, op, value in DOSSIER_DOMAIN]]
    try:
        found = odoo_call("exp.invoice", "search_read", domain, fields=fields)
    except Exception as error:
        print(f"require_invoice {invoice_id}: {error}", flush=True)
        raise ToolError("Experio est injoignable : la facture n'a pas pu etre lue.") from None
    if not found:
        raise ToolError(f"Aucune facture n° {invoice_id} dans les dossiers du cabinet.")
    return found[0]

def invoice_context(invoice):
    """Context that makes Experio validate against the invoice's own cabinet and exercice,
    not the cabinet's latest one (tarik-master odoo._exercice_context). ``invoice`` holds
    company_id and exercice_id."""
    context = {"allowed_company_ids": [invoice["company_id"][0]]}
    if invoice["exercice_id"]:
        context["cron_fiscal_entry_id"] = invoice["exercice_id"][0]
    return context


FORM_SKIP = {"one2many", "many2many", "binary", "html", "properties", "json"}

_forms = {}  # per cabinet: each database has its own fields

def fill_invoice(invoice_id, changes, context):
    """Type ``changes`` into the invoice form one field at a time, as a user does:
    after each one Experio's onchange fills what it fills on screen (exchange
    rate, withholding...). Then save, unless DRY_RUN. Returns every field that
    ends up changed, Experio's included (tarik-master odoo.fill)."""
    _form = _forms.setdefault(current(), {})
    if not _form:
        meta = odoo_call("exp.invoice", "fields_get", [], attributes=["type", "store", "readonly"])
        _form["types"] = {name: m["type"] for name, m in meta.items() if m.get("store") and m["type"] not in FORM_SKIP
                          and not name.startswith("__") and name not in ("id", "create_uid", "write_uid", "create_date", "write_date")}
        _form["editable"] = {name for name, m in meta.items() if not m.get("readonly")}
    types = _form["types"]
    record = odoo_call("exp.invoice", "read", [invoice_id], fields=list(types), context=context)[0]
    values = {k: (v[0] if types.get(k) == "many2one" and v else v) for k, v in record.items() if k in types}
    start = dict(values)
    spec = {name: {} for name in types}
    for name, value in changes.items():
        values[name] = value
        answer = odoo_json("exp.invoice", "onchange", [invoice_id], values, [name], spec, context=context)
        for key, new in ((answer or {}).get("value") or {}).items():
            if key in types:
                values[key] = new.get("id") if isinstance(new, dict) else new
    changed = {k: v for k, v in values.items() if v != start.get(k) and (k in _form["editable"] or k in changes)}
    if changed and not dry_run():
        odoo_call("exp.invoice", "write", [invoice_id], changed, context=context)
    return changed
