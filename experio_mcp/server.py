"""Standalone read-only MCP server over Experio (Odoo) cabinets, for Claude Code: it fetches the data, Claude builds
the dashboards and reports. Cabinets come from a JSON file (EXPERIO_MCP_CABINETS, default
~/.config/experio-mcp/cabinets.json, chmod 600): {"experio_cabinet_8": {"url": "https://...", "user": "...",
"key": "..."}}; use a dedicated, non-admin, read-only Experio user and an API key you can revoke.

A cabinet is one Experio database; each of its client dossiers is a ``res.company`` in it, picked with ``dossier_id``
(the ``allowed_company_ids`` context). Field names and rules below were checked against the live Experio and its code
(../experio_app). Tarik has its own copies of the report rules (engine/tools/data/registry.py and reports.py): a rule
changed there is changed here too, and the other way round."""

import csv
import functools
import json
import os
import re
import threading
import xmlrpc.client
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

CONFIG = Path(os.environ.get("EXPERIO_MCP_CABINETS", "~/.config/experio-mcp/cabinets.json")).expanduser()
OUT = Path(os.environ.get("EXPERIO_MCP_OUT", "~/experio-reports")).expanduser()
TIMEOUT = 120  # seconds, per Experio call
MAX_ROWS, BATCH, MAX_BYTES = 500, 5000, 150_000  # rows of a read; rows of an export's call; an answer's size
READS = {"search_read", "search_count", "read", "read_group", "fields_get", "name_search"}  # nothing writes
# What the generic tools never read: system settings and secrets, users, bank accounts, mail servers, attachments.
DENIED_MODELS = ("ir.", "res.users", "res.partner.bank", "res.config", "auth_", "fetchmail", "mail.", "bus.")
SECRET = re.compile(r"password|passwd|secret|token|api_key|apikey|totp|acc_number|iban", re.IGNORECASE)
PERIODS = ("day", "week", "month", "quarter", "year")
# Experio's client dossiers (engine/tools/core/config.py DOSSIER_DOMAIN).
DOSSIERS = [["enabled", "=", True], ["id", "!=", 1], ["is_deleted", "=", False]]
# Its posted journal items: the FEC's domain (engine/tools/dossiers/export.py) minus the closure entries, which
# Experio's own balance, journal and auxiliary reports leave out (account_reports/models/exp_report_balance.py:422,
# :667): a closed year's revenue would read 0 with them. Opening (carry_forward) entries stay in.
POSTED = [
    ["parent_state", "in", ["posted", "exported"]],
    ["exp_deleted", "=", False],
    ["move_id.exp_deleted", "=", False],
    ["move_id.move_category", "!=", "cloture"],
]
SALE_STATES = ["post", "export"]  # a posted invoice (exp_invoice.py:2199); cancel, pending and draft are not

mcp = FastMCP("experio")
_uids = {}  # (cabinet, url, user) -> uid: one login per cabinet and key
_local = threading.local()  # each worker thread's proxies: one connection per thread and endpoint


def tool(function):
    """Register ``function`` as an MCP tool run in a worker thread: its Experio calls never block the server."""

    @functools.wraps(function)
    async def run(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(function, *args, **kwargs))

    return mcp.tool()(run)


class _Transport(xmlrpc.client.SafeTransport):
    """HTTPS XML-RPC with a timeout (the certificate and the host name are checked, as by default)."""

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = TIMEOUT
        return connection


def _proxy(url, path):
    proxies = _local.__dict__.setdefault("proxies", {})
    if (url, path) not in proxies:
        proxies[url, path] = xmlrpc.client.ServerProxy(f"{url}{path}", transport=_Transport(), allow_none=True)
    return proxies[url, path]


def _cabinet(name):
    """Return cabinet ``name``'s settings, checked: the file is the user's alone, and the url is https, so the key
    never travels in clear."""
    try:
        if CONFIG.stat().st_mode & 0o077:
            raise ValueError(f"{CONFIG} must be readable by you alone: chmod 600 {CONFIG}")
        cabinets = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        raise ValueError(f"No cabinets file at {CONFIG}: create it (see experio_mcp/README.md)") from None
    except json.JSONDecodeError:
        raise ValueError(f"{CONFIG} is not valid JSON") from None
    if name not in cabinets:
        raise ValueError(f"Unknown cabinet {name!r}; known: {sorted(cabinets)}")
    cabinet = cabinets[name]
    if not {"url", "user", "key"} <= set(cabinet):
        raise ValueError(f"{name}: the file needs url, user and key")
    if not cabinet["url"].startswith("https://"):
        raise ValueError(f"{name}: url must be https")
    return cabinet


def _uid(cabinet, settings, url):
    """Return the Experio user id of ``cabinet``, logged in once per url, user and key."""
    found = (cabinet, url, settings["user"])
    if found not in _uids:
        uid = _proxy(url, "/xmlrpc/2/common").authenticate(cabinet, settings["user"], settings["key"], {})
        if not uid:
            raise ValueError(f"{cabinet}: login refused")
        _uids[found] = uid
    return _uids[found]


def _call(cabinet, model, method, *args, dossier_id=None, **kw):
    """One read call in ``cabinet``, inside dossier ``dossier_id`` when given.

    Experio's read_group alone keeps only the selected exercice's posted invoices and bank operations
    (exp_account/models/base.py:575-592, :332-357): a question is about any year, so the filter is skipped."""
    if method not in READS:
        raise ValueError(f"{method} is not a read")
    settings = _cabinet(cabinet)
    url = settings["url"].rstrip("/")
    uid = _uid(cabinet, settings, url)
    kw["context"] = {"skip_fiscal_year_filter": True, **({"allowed_company_ids": [dossier_id]} if dossier_id else {})}
    return _proxy(url, "/xmlrpc/2/object").execute_kw(cabinet, uid, settings["key"], model, method, list(args), kw)


def _guard(model, domain=(), fields=()):
    """Raise ValueError when the generic tools may not read ``model``, or name a field that looks like a secret."""
    if model.startswith(DENIED_MODELS):
        raise ValueError(f"{model} is not readable here (system settings, users, secrets, attachments, bank accounts)")
    named = [*fields, *(term[0] for term in domain if isinstance(term, list | tuple) and term)]
    for name in named:
        if isinstance(name, str) and SECRET.search(name):
            raise ValueError(f"{name}: a field that looks like a secret is not readable here")


def _cap(payload):
    """Return ``payload``; raise ValueError when it is longer than MAX_BYTES: it would flood the conversation."""
    size = len(json.dumps(payload, default=str))
    if size > MAX_BYTES:
        raise ValueError(
            f"The answer is {size // 1024} KB: name fewer fields, lower limit, add filters, or use export_csv."
        )
    return payload


def _fields(fields):
    """Return ``fields``; raise ValueError when empty (that would read every column, 500 KB a row on an invoice)."""
    if not fields:
        raise ValueError("Name the fields you need (describe_model lists them): an empty list reads every column.")
    return fields


def _limit(limit):
    return max(1, min(limit, MAX_ROWS))


def _dates(field, date_from, date_to):
    """Domain terms keeping ``field`` between ISO dates ``date_from`` and ``date_to`` (either may be empty)."""
    return ([[field, ">=", date_from]] if date_from else []) + ([[field, "<=", date_to]] if date_to else [])


def _prefix(prefix, required=True):
    """Domain terms keeping the accounts whose code starts with ``prefix`` (ASCII digits, up to 8)."""
    if not prefix and not required:
        return []
    if not re.fullmatch(r"[0-9]{1,8}", prefix or ""):
        raise ValueError("account_prefix: the start of an account code, in digits (6 charges, 7 produits, 342, 441)")
    return [["account_id.code", "=like", f"{prefix}%"]]


def _read(cabinet, model, domain, fields, dossier_id=None, limit=200, offset=0, order=""):
    """Return {rows, total, truncated} of ``model``'s records matching ``domain``; the total is counted only when the
    page is full."""
    limit, offset = _limit(limit), max(0, offset)
    rows = _call(
        cabinet,
        model,
        "search_read",
        domain,
        fields=fields,
        limit=limit,
        offset=offset,
        order=order,
        dossier_id=dossier_id,
    )
    total = offset + len(rows)
    if len(rows) == limit:
        total = _call(cabinet, model, "search_count", domain, dossier_id=dossier_id)
    return _cap({"rows": rows, "total": total, "truncated": offset + len(rows) < total})


def _sums(cabinet, dossier_id, domain, groupby, values=("debit", "credit"), orderby="", limit=300):
    """Return the sums of ``values`` of the posted journal items matching ``domain``, grouped by ``groupby``."""
    return _call(
        cabinet,
        "exp.move.line",
        "read_group",
        [*POSTED, *domain],
        [f"{value}:sum" for value in values],
        groupby,
        lazy=False,
        orderby=orderby,
        limit=limit,
        dossier_id=dossier_id,
    )


def _balance(row):
    """Return the debit minus the credit of a row, rounded to cents (a revenue reads negative)."""
    return round(row["debit"] - row["credit"], 2)


# --- Discovery ---


@tool
def list_cabinets() -> list[str]:
    """Names (Experio databases) of the configured cabinets. Every other tool takes one as ``cabinet``."""
    return sorted(json.loads(CONFIG.read_text()))


@tool
def whoami(cabinet: str) -> dict:
    """The logged-in Experio user of ``cabinet`` and the companies (cabinet + dossiers) it may open."""
    settings = _cabinet(cabinet)
    uid = _uid(cabinet, settings, settings["url"].rstrip("/"))
    return _call(cabinet, "res.users", "read", [uid], fields=["name", "login", "company_id", "company_ids"])[0]


@tool
def list_models(cabinet: str, query: str = "") -> dict:
    """Odoo models of a cabinet whose technical or display name contains ``query`` (e.g. "exp.invoice"), at most 500."""
    domain = ["|", ["model", "ilike", query], ["name", "ilike", query]] if query else []
    try:
        return _read(cabinet, "ir.model", domain, ["model", "name"], limit=MAX_ROWS, order="model")
    except xmlrpc.client.Fault:
        raise ValueError(
            "This login may not list models (Experio reserves it to administrators): use describe_model on a model "
            "you know, such as exp.invoice, exp.move.line, exp.account, exp.journal, exp.partner."
        ) from None


@tool
def describe_model(cabinet: str, model: str) -> dict:
    """Fields of ``model`` (type, label, relation, selection values), to pick the columns of a read."""
    _guard(model)
    return _cap(_call(cabinet, model, "fields_get", attributes=["type", "string", "relation", "selection"]))


# --- Generic reads (any model but the denied ones, columns named) ---


@tool
def count(cabinet: str, model: str, domain: list | None = None, dossier_id: int | None = None) -> int:
    """Number of ``model`` records matching ``domain`` (Odoo syntax, e.g. [["state","=","post"]]), in a dossier."""
    _guard(model, domain or [])
    return _call(cabinet, model, "search_count", domain or [], dossier_id=dossier_id)


@tool
def search_read(
    cabinet: str,
    model: str,
    fields: list[str],
    domain: list | None = None,
    dossier_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    order: str = "",
) -> dict:
    """Records of ``model`` matching ``domain``, only the ``fields`` named (describe_model lists them: an empty list
    is refused, one invoice has 284 columns). Returns {rows, total, truncated}; ``limit`` is capped at 500 and an
    answer over 150 KB is refused: page with ``offset``, narrow the fields, or use export_csv."""
    _guard(model, domain or [], _fields(fields))
    return _read(cabinet, model, domain or [], fields, dossier_id, limit, offset, order)


@tool
def get_records(
    cabinet: str, model: str, ids: list[int], fields: list[str], dossier_id: int | None = None
) -> list[dict]:
    """The ``fields`` named of the ``ids`` of ``model`` (at most 500 ids)."""
    _guard(model, (), _fields(fields))
    return _cap(_call(cabinet, model, "read", ids[:MAX_ROWS], fields=fields, dossier_id=dossier_id))


@tool
def name_search(cabinet: str, model: str, name: str, dossier_id: int | None = None, limit: int = 20) -> list:
    """[id, display name] pairs of ``model`` records whose name matches ``name`` (partners, accounts, journals...)."""
    _guard(model)
    return _call(cabinet, model, "name_search", name, limit=_limit(limit), dossier_id=dossier_id)


@tool
def read_group(
    cabinet: str,
    model: str,
    fields: list[str],
    groupby: list[str],
    domain: list | None = None,
    dossier_id: int | None = None,
    orderby: str = "",
    limit: int = 300,
) -> list[dict]:
    """Aggregates: ``fields`` like ["debit:sum","credit:sum"], ``groupby`` like ["date:month","account_id"], at most
    ``limit`` groups (300 by default). Experio's selected-exercice filter is skipped: any year is read."""
    _guard(model, domain or [], [name.split(":")[0] for name in [*fields, *groupby]])
    return _cap(
        _call(
            cabinet,
            model,
            "read_group",
            domain or [],
            fields,
            groupby,
            lazy=False,
            orderby=orderby,
            limit=_limit(limit),
            dossier_id=dossier_id,
        )
    )


def _cell(value):
    """Return a value as a CSV cell: empty for false, « name [id] » for a many2one, text a spreadsheet can't run."""
    if value is False or value is None:
        return ""
    if isinstance(value, list | tuple):
        value = (
            f"{value[1]} [{value[0]}]"
            if len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str)
            else json.dumps(value)
        )
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value  # a formula in a client's name must stay text
    return value


@tool
def export_csv(
    cabinet: str,
    model: str,
    fields: list[str],
    filename: str,
    domain: list | None = None,
    dossier_id: int | None = None,
) -> str:
    """Write every matching record (sorted by id) to a new CSV under the reports folder (EXPERIO_MCP_OUT, default
    ~/experio-reports), read in batches of 5000 without passing the rows through the chat; returns the path and the row
    count. For big extracts Claude then loads with code (pandas...) to build a dashboard. The ``fields`` are named
    (empty is refused); the file is yours alone and never overwrites another."""
    _guard(model, domain or [], _fields(fields))
    name = re.sub(r"[^A-Za-z0-9._-]", "-", Path(filename).name).strip(".") or "export"
    path = OUT / (name if name.endswith(".csv") else f"{name}.csv")
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(f"{path} already exists: choose another filename") from None
    wanted, last, total = fields, 0, 0
    with os.fdopen(descriptor, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(wanted)
        while True:
            rows = _call(
                cabinet,
                model,
                "search_read",
                [*(domain or []), ["id", ">", last]],
                fields=sorted({*wanted, "id"}),
                order="id",
                limit=BATCH,
                dossier_id=dossier_id,
            )
            writer.writerows([_cell(row.get(field)) for field in wanted] for row in rows)
            total += len(rows)
            if len(rows) < BATCH:
                return f"{path}: {total} rows"
            last = rows[-1]["id"]


# --- Experio: dossiers, fiscal years, chart of accounts, journals, partners ---


@tool
def list_dossiers(cabinet: str, query: str = "") -> dict:
    """The client dossiers this login can open (id, name, ICE, RC) whose name contains ``query``. ``dossier_id``
    elsewhere. Dossiers the account may not open are left out: every tool would fail on them."""
    settings = _cabinet(cabinet)
    uid = _uid(cabinet, settings, settings["url"].rstrip("/"))
    ids = _call(cabinet, "res.users", "read", [uid], fields=["company_ids"])[0]["company_ids"]
    domain = [*DOSSIERS, ["id", "in", ids], *([["name", "ilike", query]] if query else [])]
    return _read(cabinet, "res.company", domain, ["name", "ice", "rc"], limit=MAX_ROWS, order="name")


@tool
def list_fiscal_years(cabinet: str, dossier_id: int) -> dict:
    """Fiscal years (exp.fiscal.entries: name, dates, state) of a dossier. States: draft; open; close (accounts
    stopped, closure entries not generated); shut (closure entries and opening entries generated)."""
    domain = [["company_id", "=", dossier_id]]
    return _read(
        cabinet,
        "exp.fiscal.entries",
        domain,
        ["name", "date_start", "date_end", "state"],
        dossier_id,
        MAX_ROWS,
        order="date_end desc",
    )


@tool
def list_accounts(cabinet: str, dossier_id: int, query: str = "") -> dict:
    """The dossier's chart of accounts (code, name), filtered by ``query`` on code or name."""
    domain = [["company_id", "=", dossier_id], ["exp_deleted", "=", False]]
    domain += ["|", ["code", "=like", f"{query}%"], ["name", "ilike", query]] if query else []
    return _read(cabinet, "exp.account", domain, ["code", "name"], dossier_id, MAX_ROWS, order="code")


@tool
def list_journals(cabinet: str, dossier_id: int) -> dict:
    """The dossier's journals (name, code, type: sale/purchase/bank/cash/general)."""
    domain = [["company_id", "=", dossier_id], ["exp_deleted", "=", False]]
    return _read(cabinet, "exp.journal", domain, ["name", "code", "type"], dossier_id, MAX_ROWS, order="name")


@tool
def list_partners(cabinet: str, dossier_id: int, query: str = "", limit: int = 200) -> dict:
    """The dossier's third parties (clients, suppliers) whose name contains ``query``."""
    domain = [["company_id", "=", dossier_id], ["exp_deleted", "=", False]]
    domain += [["name", "ilike", query]] if query else []
    return _read(cabinet, "exp.partner", domain, ["name"], dossier_id, limit, order="name")


# --- Experio: ready reports over posted journal items (exp.move.line) ---
# Amounts are in the dossier's currency. A balance is debit minus credit: a revenue reads negative. Closure entries
# are left out; opening (à-nouveau) entries are in: a balance-sheet account read for one exercice from an exercice
# that holds no opening entry shows its movements only, and one read from the origin adds every year.


def _lines(dossier_id, date_from, date_to, extra=()):
    return [["company_id", "=", dossier_id], *_dates("date", date_from, date_to), *extra]


@tool
def trial_balance(
    cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "", account_prefix: str = "", limit: int = 300
) -> dict:
    """Trial balance: debit, credit and balance per account over the posted items between two ISO dates, for the
    accounts whose code starts with ``account_prefix`` ("6" charges, "7" produits, "61" achats, "342" clients, "441"
    fournisseurs, "5" trésorerie; empty: all). Returns {rows, truncated}: past ``limit`` accounts the list is cut in
    code order and gives no total."""
    domain = _lines(dossier_id, date_from, date_to, _prefix(account_prefix, required=False))
    rows = _sums(cabinet, dossier_id, domain, ["account_id"], orderby="account_id", limit=limit + 1)
    shown = [
        {
            "account": row["account_id"],
            "debit": row["debit"],
            "credit": row["credit"],
            "balance": _balance(row),
            "lines": row["__count"],
        }
        for row in rows[:limit]
    ]
    return _cap({"rows": shown, "truncated": len(rows) > limit})


@tool
def monthly_totals(cabinet: str, dossier_id: int, account_prefix: str, date_from: str = "", date_to: str = "") -> list:
    """Debit, credit and balance per month for the accounts starting with ``account_prefix`` (revenue "71", purchases
    and external charges "61" ("611" is only the goods resold), VAT "3455" recoverable, "4455" collected, "4456" due):
    the series behind trend charts. A month with no entry is absent."""
    domain = _lines(dossier_id, date_from, date_to, _prefix(account_prefix))
    rows = _sums(cabinet, dossier_id, domain, ["date:month"], orderby="date:month")
    return [
        {"month": row["date:month"], "debit": row["debit"], "credit": row["credit"], "balance": _balance(row)}
        for row in rows
    ]


@tool
def general_ledger(
    cabinet: str,
    dossier_id: int,
    account_prefix: str,
    date_from: str = "",
    date_to: str = "",
    partner_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Journal items of the accounts starting with ``account_prefix`` (optionally one third party), oldest first:
    date, journal, entry, label, partner, debit, credit, reconciled (fully). For a whole ledger use export_csv."""
    extra = [*_prefix(account_prefix), *([["partner_id", "=", partner_id]] if partner_id else [])]
    fields = ["date", "journal_id", "move_id", "name", "account_id", "partner_id", "debit", "credit", "reconciled"]
    return _read(
        cabinet,
        "exp.move.line",
        [*POSTED, *_lines(dossier_id, date_from, date_to, extra)],
        fields,
        dossier_id,
        limit,
        offset,
        "date, id",
    )


@tool
def partner_balances(
    cabinet: str, dossier_id: int, account_prefix: str, date_from: str = "", date_to: str = "", limit: int = 300
) -> dict:
    """What is still open per third party on the accounts starting with ``account_prefix`` ("342" clients, "441"
    suppliers): Experio's own residual (amount_residual: a part payment counts what is left) of the items not fully
    reconciled, biggest first. It follows today's reconciliation. From the origin by default: for an exercice that holds
    opening (à-nouveau) entries pass its start as ``date_from``, or carried items are counted twice. Returns {rows,
    truncated}: past ``limit`` third parties the list is cut in name order and its total is not given."""
    extra = [*_prefix(account_prefix), ["amount_residual", "!=", 0]]
    domain = _lines(dossier_id, date_from, date_to, extra)
    rows = _sums(cabinet, dossier_id, domain, ["partner_id"], ("amount_residual",), limit=limit + 1)
    shown = [{"partner": row["partner_id"], "balance": round(row["amount_residual"], 2)} for row in rows[:limit]]
    shown.sort(key=lambda row: -abs(row["balance"]))
    return _cap({"rows": shown, "truncated": len(rows) > limit})


@tool
def journal_totals(cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "") -> list:
    """Debit, credit and entry-line count per journal over the period."""
    rows = _sums(cabinet, dossier_id, _lines(dossier_id, date_from, date_to), ["journal_id"])
    return [
        {"journal": row["journal_id"], "debit": row["debit"], "credit": row["credit"], "lines": row["__count"]}
        for row in rows
    ]


# --- Experio: invoices and bank statements ---
# An invoice's amounts are in its own currency (currency_id): group by it before adding. A credit note (refund true) has
# positive amounts that are to be subtracted (exp_invoice.py:2015-2021; display_amount_total negates them, :2620-2630).

INVOICE = [
    "invoice_number",
    "invoice_date",
    "partner_id",
    "type",
    "refund",
    "currency_id",
    "amount_untaxed",
    "amount_tva",
    "amount_total",
    "amount_residual",
    "payment_state",
    "state",
]


@tool
def invoices(
    cabinet: str,
    dossier_id: int,
    date_from: str = "",
    date_to: str = "",
    state: str = "",
    unpaid_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Invoices (exp.invoice) of a dossier by ``invoice_date``, deleted and abandoned ones left out: number, date,
    partner, type (purchase/sale), refund (a credit note: positive amounts to subtract), currency, untaxed, VAT, total,
    residual, payment state, state. ``state``: draft, pending, post, export (empty: all four); ``unpaid_only`` keeps
    posted invoices (post, export) not fully paid."""
    domain = [["company_id", "=", dossier_id], ["exp_deleted", "=", False], *_dates("invoice_date", date_from, date_to)]
    domain += [["state", "=", state]] if state else [["state", "!=", "cancel"]]
    if unpaid_only:
        domain += [["state", "in", SALE_STATES], ["payment_state", "!=", "paid"]]
    return _read(cabinet, "exp.invoice", domain, INVOICE, dossier_id, limit, offset, "invoice_date desc, id desc")


@tool
def invoice_totals(
    cabinet: str, dossier_id: int, date_from: str = "", date_to: str = "", period: str = "month"
) -> list:
    """Count, untaxed, VAT, total and residual per ``period`` (day/week/month/quarter/year) of invoice_date, by type
    (purchase/sale), refund (credit note) and currency, posted invoices only. Each row also gives ``net_untaxed`` and
    ``net_total``: the amounts, negated for a credit note, so the rows of one type and currency add up."""
    if period not in PERIODS:
        raise ValueError(f"period: one of {', '.join(PERIODS)}")
    domain = [
        ["company_id", "=", dossier_id],
        ["exp_deleted", "=", False],
        ["state", "in", SALE_STATES],
        *_dates("invoice_date", date_from, date_to),
    ]
    group = f"invoice_date:{period}"
    rows = _call(
        cabinet,
        "exp.invoice",
        "read_group",
        domain,
        ["amount_untaxed:sum", "amount_tva:sum", "amount_total:sum", "amount_residual:sum"],
        [group, "type", "refund", "currency_id"],
        lazy=False,
        orderby=group,
        limit=MAX_ROWS,
        dossier_id=dossier_id,
    )
    keep = ("amount_untaxed", "amount_tva", "amount_total", "amount_residual")
    result = []
    for row in rows:
        sign = -1 if row["refund"] else 1
        result.append(
            {
                "period": row[group],
                "type": row["type"],
                "refund": row["refund"],
                "currency": row["currency_id"],
                "count": row["__count"],
                **{name: row[name] for name in keep},
                "net_untaxed": sign * row["amount_untaxed"],
                "net_total": sign * row["amount_total"],
            }
        )
    return _cap(result)


BANK = ["date", "payment_ref", "amount", "debit", "credit", "state", "journal_id", "partner_id"]


@tool
def bank_operations(
    cabinet: str,
    dossier_id: int,
    date_from: str = "",
    date_to: str = "",
    state: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Bank statement lines (exp.bank.statement.line) of a dossier, deleted ones left out: date, label, amount (the
    credit minus the debit of the statement, in the statement's currency), state (pending, draft, ready, post, export;
    suspense is a legacy state from the migration), journal, partner. ``state`` keeps one state."""
    domain = [["company_id", "=", dossier_id], ["exp_deleted", "=", False], *_dates("date", date_from, date_to)]
    domain += [["state", "=", state]] if state else []
    return _read(cabinet, "exp.bank.statement.line", domain, BANK, dossier_id, limit, offset, "date desc, id desc")


if __name__ == "__main__":
    mcp.run()
