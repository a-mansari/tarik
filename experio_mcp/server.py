"""Standalone read-only MCP server over Experio (Odoo) cabinets, for Claude Code: it fetches the data, Claude builds
the dashboards and reports. Cabinets come from a JSON file (EXPERIO_MCP_CABINETS, default
~/.config/experio-mcp/cabinets.json): {"experio_cabinet_8": {"url": "https://...", "user": "...", "key": "..."}}.

A cabinet is one Experio database; each of its client dossiers is a ``res.company`` in it, picked with ``dossier_id``
(the ``allowed_company_ids`` context). Field names below are those Tarik's own code proved against Experio."""

import csv
import json
import os
import socket
import xmlrpc.client
from pathlib import Path

from mcp.server.fastmcp import FastMCP

socket.setdefaulttimeout(300)
CONFIG = Path(os.environ.get("EXPERIO_MCP_CABINETS", "~/.config/experio-mcp/cabinets.json")).expanduser()
OUT = Path(os.environ.get("EXPERIO_MCP_OUT", "~/experio-reports")).expanduser()
MAX_ROWS = 2000
READS = {"search_read", "search_count", "read", "read_group", "fields_get", "name_search"}  # nothing writes
# Experio's client dossiers (engine/tools/core/config.py DOSSIER_DOMAIN) and its posted, undeleted journal items
# (the FEC's domain, engine/tools/dossiers/export.py).
DOSSIERS = [["enabled", "=", True], ["id", "!=", 1], ["is_deleted", "=", False]]
POSTED = [["parent_state", "in", ["posted", "exported"]], ["exp_deleted", "=", False], ["move_id.exp_deleted", "=", False]]

mcp = FastMCP("experio")
_uids = {}  # cabinet -> uid: one login per cabinet


def _cabinet(name):
    """Return cabinet ``name``'s settings, checked: https only, so the key never travels in clear."""
    try:
        cabinets = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        raise ValueError(f"No cabinets file at {CONFIG}: create it (see experio_mcp/README.md)") from None
    if name not in cabinets:
        raise ValueError(f"Unknown cabinet {name!r}; known: {sorted(cabinets)}")
    cabinet = cabinets[name]
    if not cabinet["url"].startswith("https://"):
        raise ValueError(f"{name}: url must be https")
    return cabinet


def _call(cabinet, model, method, *args, dossier_id=None, **kw):
    """One read call in ``cabinet``, inside dossier ``dossier_id`` when given."""
    if method not in READS:
        raise ValueError(f"{method} is not a read")
    settings = _cabinet(cabinet)
    url = settings["url"].rstrip("/")
    if cabinet not in _uids:
        uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(
            cabinet, settings["user"], settings["key"], {}
        )
        if not uid:
            raise ValueError(f"{cabinet}: login refused")
        _uids[cabinet] = uid
    if dossier_id:
        kw["context"] = {"allowed_company_ids": [dossier_id]}
    proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return proxy.execute_kw(cabinet, _uids[cabinet], settings["key"], model, method, list(args), kw)


def _dates(field, date_from, date_to):
    """Domain terms keeping ``field`` between ISO dates ``date_from`` and ``date_to`` (either may be empty)."""
    return ([[field, ">=", date_from]] if date_from else []) + ([[field, "<=", date_to]] if date_to else [])


def _read(cabinet, model, domain, fields, dossier_id=None, limit=200, offset=0, order=""):
    limit = min(limit, MAX_ROWS)
    kw = {"fields": fields, "limit": limit, "offset": offset, "order": order, "dossier_id": dossier_id}
    rows = _call(cabinet, model, "search_read", domain, **kw)
    total = _call(cabinet, model, "search_count", domain, dossier_id=dossier_id)
    return {"rows": rows, "total": total, "truncated": offset + len(rows) < total}


# --- Discovery ---


@mcp.tool()
def list_cabinets() -> list[str]:
    """Names (Experio databases) of the configured cabinets. Every other tool takes one as ``cabinet``."""
    return sorted(json.loads(CONFIG.read_text()))


@mcp.tool()
def whoami(cabinet: str) -> dict:
    """The logged-in Experio user of ``cabinet`` and the companies (cabinet + dossiers) it may open."""
    return _call(cabinet, "res.users", "search_read", [["id", "=", _uids.get(cabinet) or _login(cabinet)]],
                 fields=["name", "login", "company_id", "company_ids"])[0]


def _login(cabinet):
    _call(cabinet, "res.company", "search_count", [])  # logs in
    return _uids[cabinet]


@mcp.tool()
def list_models(cabinet: str, query: str = "") -> list[dict]:
    """Odoo models of a cabinet whose technical or display name contains ``query`` (e.g. "exp.invoice")."""
    domain = ["|", ["model", "ilike", query], ["name", "ilike", query]] if query else []
    return _call(cabinet, "ir.model", "search_read", domain, fields=["model", "name"], limit=300)


@mcp.tool()
def describe_model(cabinet: str, model: str) -> dict:
    """Fields of ``model`` (type, label, relation, selection values), to build domains and pick columns."""
    return _call(cabinet, model, "fields_get", attributes=["type", "string", "relation", "selection", "store"])


# --- Generic reads (any model, any field) ---


@mcp.tool()
def count(cabinet: str, model: str, domain: list | None = None, dossier_id: int | None = None) -> int:
    """Number of ``model`` records matching ``domain`` (Odoo syntax, e.g. [["state","=","post"]]), in a dossier."""
    return _call(cabinet, model, "search_count", domain or [], dossier_id=dossier_id)


@mcp.tool()
def search_read(
    cabinet: str, model: str, domain: list | None = None, fields: list[str] | None = None,
    dossier_id: int | None = None, limit: int = 200, offset: int = 0, order: str = "",
) -> dict:
    """Records of ``model`` matching ``domain``. Returns {rows, total, truncated}; ``limit`` is capped at 2000
    (page with ``offset``, or use export_csv for more). Name the ``fields`` you need."""
    return _read(cabinet, model, domain or [], fields or [], dossier_id, limit, offset, order)


@mcp.tool()
def get_records(cabinet: str, model: str, ids: list[int], fields: list[str] | None = None,
                dossier_id: int | None = None) -> list[dict]:
    """Records of ``model`` by id."""
    return _call(cabinet, model, "read", ids, fields=fields or [], dossier_id=dossier_id)


@mcp.tool()
def name_search(cabinet: str, model: str, name: str, dossier_id: int | None = None, limit: int = 20) -> list:
    """[id, display name] pairs of ``model`` records whose name matches ``name`` (partners, accounts, journals...)."""
    return _call(cabinet, model, "name_search", name, limit=limit, dossier_id=dossier_id)


@mcp.tool()
def read_group(
    cabinet: str, model: str, fields: list[str], groupby: list[str], domain: list | None = None,
    dossier_id: int | None = None, orderby: str = "",
) -> list[dict]:
    """Aggregates: ``fields`` like ["debit:sum","credit:sum"], ``groupby`` like ["date:month","account_id"]."""
    return _call(cabinet, model, "read_group", domain or [], fields, groupby, lazy=False, orderby=orderby,
                 dossier_id=dossier_id)


@mcp.tool()
def export_csv(
    cabinet: str, model: str, fields: list[str], filename: str, domain: list | None = None,
    dossier_id: int | None = None, order: str = "",
) -> str:
    """Write every matching record to a CSV under the reports folder (EXPERIO_MCP_OUT, default ~/experio-reports),
    without passing the rows through the chat; returns the path and row count. For big extracts and data Claude
    then loads with code (pandas...) to build a dashboard."""
    rows = _call(cabinet, model, "search_read", domain or [], fields=fields, order=order, dossier_id=dossier_id)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / Path(filename).name  # a bare name: never outside the folder
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: (f"{v[1]} [{v[0]}]" if isinstance(v, list) and len(v) == 2 else v) for k, v in row.items()
                          if k in fields} for row in rows)
    return f"{path}: {len(rows)} rows"


# --- Experio: dossiers, fiscal years, chart of accounts, journals, partners ---


@mcp.tool()
def list_dossiers(cabinet: str, query: str = "") -> dict:
    """The cabinet's client dossiers (id, name, ICE, RC) matching ``query`` in their name. ``dossier_id`` elsewhere."""
    domain = [*DOSSIERS, *([["name", "ilike", query]] if query else [])]
    return _read(cabinet, "res.company", domain, ["name", "ice", "rc"], limit=MAX_ROWS, order="name")


@mcp.tool()
def list_fiscal_years(cabinet: str, dossier_id: int | None = None) -> dict:
    """Fiscal years (exp.fiscal.entries: name, dates, state open/closed) of a dossier, or of all dossiers."""
    domain = [["company_id", "=", dossier_id]] if dossier_id else []
    return _read(cabinet, "exp.fiscal.entries", domain, ["company_id", "name", "date_start", "date_end", "state"],
                 dossier_id, MAX_ROWS, order="company_id, date_end desc")


@mcp.tool()
def list_accounts(cabinet: str, dossier_id: int, query: str = "") -> dict:
    """The dossier's chart of accounts (code, name), filtered by ``query`` on code or name."""
    domain = [["company_id", "=", dossier_id], *(["|", ["code", "=like", f"{query}%"], ["name", "ilike", query]]
                                                 if query else [])]
    return _read(cabinet, "exp.account", domain, ["code", "name"], dossier_id, MAX_ROWS, order="code")


@mcp.tool()
def list_journals(cabinet: str, dossier_id: int) -> dict:
    """The dossier's journals (name, code, type: sale/purchase/bank/cash/general)."""
    return _read(cabinet, "exp.journal", [["company_id", "=", dossier_id]], ["name", "code", "type"], dossier_id,
                 MAX_ROWS, order="name")


@mcp.tool()
def list_partners(cabinet: str, dossier_id: int, query: str = "", limit: int = 200) -> dict:
    """The dossier's third parties (clients, suppliers) whose name contains ``query``."""
    domain = [["company_id", "=", dossier_id], *([["name", "ilike", query]] if query else [])]
    return _read(cabinet, "exp.partner", domain, ["name"], dossier_id, limit, order="name")


# --- Experio: ready reports over posted journal items (exp.move.line) ---


def _lines(dossier_id, date_from, date_to, extra=()):
    return [["company_id", "=", dossier_id], *POSTED, *_dates("date", date_from, date_to), *extra]


@mcp.tool()
def trial_balance(cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "", account_prefix: str = "") -> list:
    """Trial balance: debit, credit and balance per account, posted entries between two ISO dates, accounts whose
    code starts with ``account_prefix`` (e.g. "6" expenses, "7" income, "34" customers)."""
    extra = [["account_id.code", "=like", f"{account_prefix}%"]] if account_prefix else []
    groups = _call(cabinet, "exp.move.line", "read_group", _lines(dossier_id, date_from, date_to, extra),
                   ["debit:sum", "credit:sum"], ["account_id"], lazy=False, orderby="account_id", dossier_id=dossier_id)
    return [{"account": g["account_id"], "debit": g["debit"], "credit": g["credit"],
             "balance": round(g["debit"] - g["credit"], 2), "lines": g["__count"]} for g in groups]


@mcp.tool()
def monthly_totals(cabinet: str, dossier_id: int, account_prefix: str, date_from: str = "", date_to: str = "") -> list:
    """Debit, credit and balance per month for accounts starting with ``account_prefix`` (revenue "7", purchases
    "611", VAT "345"...): the series behind trend charts."""
    domain = _lines(dossier_id, date_from, date_to, [["account_id.code", "=like", f"{account_prefix}%"]])
    groups = _call(cabinet, "exp.move.line", "read_group", domain, ["debit:sum", "credit:sum"], ["date:month"],
                   lazy=False, orderby="date:month", dossier_id=dossier_id)
    return [{"month": g["date:month"], "debit": g["debit"], "credit": g["credit"],
             "balance": round(g["debit"] - g["credit"], 2)} for g in groups]


@mcp.tool()
def general_ledger(cabinet: str, dossier_id: int, account_prefix: str, date_from: str = "", date_to: str = "",
                   partner_id: int | None = None, limit: int = 500, offset: int = 0) -> dict:
    """Journal items of the accounts starting with ``account_prefix`` (optionally one third party), oldest first:
    date, journal, entry, label, partner, debit, credit, reconciled."""
    extra = [["account_id.code", "=like", f"{account_prefix}%"], *([["partner_id", "=", partner_id]] if partner_id else [])]
    fields = ["date", "journal_id", "move_id", "name", "account_id", "partner_id", "debit", "credit", "reconciled"]
    return _read(cabinet, "exp.move.line", _lines(dossier_id, date_from, date_to, extra), fields, dossier_id, limit,
                 offset, "date, id")


@mcp.tool()
def partner_balances(cabinet: str, dossier_id: int, account_prefix: str, date_to: str = "", open_only: bool = True) -> list:
    """Balance per third party on accounts starting with ``account_prefix`` (customers "3421", suppliers "4411"),
    up to ``date_to``; ``open_only`` keeps unreconciled items (what is still owed). Biggest balance first."""
    extra = [["account_id.code", "=like", f"{account_prefix}%"], *([["reconciled", "=", False]] if open_only else [])]
    groups = _call(cabinet, "exp.move.line", "read_group", _lines(dossier_id, "", date_to, extra),
                   ["debit:sum", "credit:sum"], ["partner_id"], lazy=False, dossier_id=dossier_id)
    rows = [{"partner": g["partner_id"], "debit": g["debit"], "credit": g["credit"],
             "balance": round(g["debit"] - g["credit"], 2)} for g in groups]
    return sorted(rows, key=lambda r: -abs(r["balance"]))


@mcp.tool()
def journal_totals(cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "") -> list:
    """Debit, credit and entry-line count per journal over the period."""
    groups = _call(cabinet, "exp.move.line", "read_group", _lines(dossier_id, date_from, date_to),
                   ["debit:sum", "credit:sum"], ["journal_id"], lazy=False, dossier_id=dossier_id)
    return [{"journal": g["journal_id"], "debit": g["debit"], "credit": g["credit"], "lines": g["__count"]}
            for g in groups]


# --- Experio: invoices and bank statements ---


@mcp.tool()
def invoices(cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "", state: str = "",
             unpaid_only: bool = False, limit: int = 200, offset: int = 0) -> dict:
    """Invoices (exp.invoice) of a dossier by ``invoice_date``: number, date, partner, type, untaxed, VAT, total,
    residual, payment state (unpaid/partial/paid), state. ``state``: draft, pending, post, export, cancel;
    ``unpaid_only`` keeps amount_residual > 0. Use search_read for other columns."""
    domain = [["company_id", "=", dossier_id], *_dates("invoice_date", date_from, date_to),
              *([["state", "=", state]] if state else []), *([["amount_residual", ">", 0]] if unpaid_only else [])]
    fields = ["invoice_number", "invoice_date", "partner_id", "type", "amount_untaxed", "amount_tva", "amount_total",
              "amount_residual", "payment_state", "state"]
    return _read(cabinet, "exp.invoice", domain, fields, dossier_id, limit, offset, "invoice_date desc, id desc")


@mcp.tool()
def invoice_totals(cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "", period: str = "month") -> list:
    """Untaxed, VAT, total and unpaid residual per ``period`` (day/week/month/quarter/year) of invoice_date and
    invoice ``type``, posted invoices only."""
    domain = [["company_id", "=", dossier_id], ["state", "in", ["post", "export"]],
              *_dates("invoice_date", date_from, date_to)]
    group = f"invoice_date:{period}"
    fields = ["amount_untaxed:sum", "amount_tva:sum", "amount_total:sum", "amount_residual:sum"]
    return _call(cabinet, "exp.invoice", "read_group", domain, fields, [group, "type"], lazy=False, orderby=group,
                 dossier_id=dossier_id)


@mcp.tool()
def bank_operations(cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "", state: str = "",
                    limit: int = 200, offset: int = 0) -> dict:
    """Bank statement lines (exp.bank.statement.line) of a dossier, not deleted; ``state`` "post"/"export" to keep
    booked ones. Use describe_model for the columns, then search_read to pick them."""
    domain = [["company_id", "=", dossier_id], ["exp_deleted", "=", False], *_dates("date", date_from, date_to),
              *([["state", "=", state]] if state else [])]
    return _read(cabinet, "exp.bank.statement.line", domain, [], dossier_id, limit, offset, "date desc, id desc")


if __name__ == "__main__":
    mcp.run()
