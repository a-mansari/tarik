"""The everyday lists, built on the registry's query: who the user is and which dossiers they may open, the exercices,
the invoices (credit notes apart), the bank operations. Each is one query over the registry, so it has the same
limits."""

from experio_client_mcp.experio import DOSSIERS, SESSION, Refusal, experio_errors, odoo_call
from experio_client_mcp.query import query_data

INVOICE = [
    "invoice_number", "invoice_date", "partner_id", "type", "refund", "currency_id", "amount_untaxed", "amount_tva",
    "amount_total", "amount_residual", "payment_state", "state",
]  # fmt: skip
BANK = ["date", "payment_ref", "amount", "debit", "credit", "state", "journal_id", "partner_id"]
POSTED = ["post", "export"]  # a posted invoice (exp_invoice.py:2199); pending and draft are not
AMOUNTS = ["amount_untaxed", "amount_tva", "amount_total", "amount_residual"]
PERIODS = ("day", "week", "month", "quarter", "year")


def dossiers(query=""):
    """Return {cabinet, user, dossiers}: the client dossiers this user may open (id, name), named by ``query`` when
    given."""
    if not isinstance(query, str) or len(query) > 80:
        raise Refusal("query : un texte de 80 caractères au plus.")
    session = SESSION.get()
    with experio_errors("la lecture des dossiers"):
        user = odoo_call("res.users", "read", [session.uid], fields=["name", "company_ids"])[0]
        domain = [("id", "in", user["company_ids"]), *DOSSIERS, *([("name", "ilike", query)] if query else [])]
        rows = odoo_call("res.company", "search_read", domain, fields=["name"], order="name", limit=200)
    return {
        "cabinet": session.cabinet,
        "user": user["name"],
        "dossiers": [{"id": r["id"], "name": r["name"]} for r in rows],
    }


def fiscal_years(dossier_id):
    """Return the dossier's exercices (name, dates, state), latest first."""
    return query_data(
        dossier_id,
        "exp.fiscal.entries",
        fields=["name", "date_start", "date_end", "state"],
        order="date_end desc",
        limit=50,
    )


def dates(field, date_from, date_to):
    """Return the filters keeping ``field`` between two ISO dates (either may be empty); the registry checks
    them."""
    return ([[field, ">=", date_from]] if date_from else []) + ([[field, "<=", date_to]] if date_to else [])


def invoices(dossier_id, date_from="", date_to="", state="", unpaid_only=False, limit=50):
    """Return the dossier's invoices by invoice_date, newest first (deleted and abandoned ones are never listed)."""
    domain = dates("invoice_date", date_from, date_to) + ([["state", "=", state]] if state else [])
    if unpaid_only:
        domain += [["state", "in", POSTED], ["payment_state", "!=", "paid"]]
    return query_data(dossier_id, "exp.invoice", domain, INVOICE, order="invoice_date desc", limit=limit)


def invoice_totals(dossier_id, date_from="", date_to="", period="month"):
    """Return the posted invoices' count, untaxed, VAT, total and residual per period, type, credit note and currency;
    each row adds net_untaxed and net_total (negated for a credit note, so the rows of one type and currency add up)."""
    if period not in PERIODS:
        raise Refusal(f"period : {', '.join(PERIODS)}")
    group = f"invoice_date:{period}"
    domain = [["state", "in", POSTED], *dates("invoice_date", date_from, date_to)]
    found = query_data(
        dossier_id,
        "exp.invoice",
        domain,
        group_by=[group, "type", "refund", "currency_id"],
        sums=AMOUNTS,
        order=group,
        limit=200,
        group_limit=4,
    )
    for row in found["groups"]:
        sign = -1 if row.get("refund") else 1
        row["net_untaxed"], row["net_total"] = (
            round(sign * row.get("amount_untaxed", 0), 2),
            round(sign * row.get("amount_total", 0), 2),
        )
    return found


def bank_operations(dossier_id, date_from="", date_to="", state="", limit=50):
    """Return the dossier's bank statement lines, newest first (deleted ones are never listed)."""
    domain = dates("date", date_from, date_to) + ([["state", "=", state]] if state else [])
    return query_data(dossier_id, "exp.bank.statement.line", domain, BANK, order="date desc", limit=limit)
