"""Charges (or produits) constatees d'avance -- a billed period over several months.

Bluejay doesn't detect the period; check_invoice reads it on the scan. When it spans several
months, the client is asked whether to spread the charge. Experio does the rest:
ticking it in the form fills and validates the period, and posting generates the spreading
entries with the dossier's prepaid account and journal.
"""

from tarik.core.app import ok, question, routine, step_tool
from tarik.core.conversation import kept_yes_no
from tarik.core.experio import (
    fill_invoice,
    invoice_context,
    refused,
    require_draft,
    require_invoice,
    test_mode_note,
)
from tarik.invoices.check import stored_check


def prepaid_period(start, end):
    """The billed period from ``start`` to ``end`` (YYYY-MM-DD) as Experio's prepaid fields, when it spans several
    months, else None."""
    if not (start and end and len(start) == len(end) == 10 and start < end):
        return None
    months = month_number(end) - month_number(start) + 1
    if months < 2:
        return None
    return {"prepaid_start_date": start, "prepaid_end_date": end, "prepaid_months": months}


def month_number(day):
    """``day`` (YYYY-MM-DD) as a count of months, to subtract one from another."""
    return int(day[:4]) * 12 + int(day[5:7])  # its year, then its month


@step_tool
def settle_prepaid(invoice_id: int, answer: str = "") -> dict:
    """When a draft invoice bills a period over several months, ask whether to spread it
    (charges or produits constates d'avance); call again with ``answer`` "oui" or "non", as the agent read the
    client's reply."""
    invoice = require_invoice(
        invoice_id, ["state", "type", "company_id", "exercice_id", "invoice_number", "has_prepaid_expenses"]
    )
    if invoice["has_prepaid_expenses"]:
        return routine(f"Facture {invoice_id} : déjà répartie en constatée d'avance.")
    check = stored_check(invoice_id)
    values = check and prepaid_period(check[1].get("period_start"), check[1].get("period_end"))
    if not values:
        return routine(f"Facture {invoice_id} : pas de période sur plusieurs mois, rien à répartir.")
    kind = "produits" if invoice["type"] == "sale" else "charges"
    asked = (
        f"Facture {invoice['invoice_number'] or invoice_id} : elle couvre {values['prepaid_months']} mois "
        f"({values['prepaid_start_date']} au {values['prepaid_end_date']}). "
        f"La répartir en {kind} constates d'avance ? oui / non"
    )
    # Asked before: its answer, not the question again.
    answer, spread = kept_yes_no(invoice_id, "settle_prepaid", answer)
    if spread is None:
        return question(asked if not answer else f"Réponse non comprise. {asked}")
    if not spread:
        return routine(f"Facture {invoice_id} : pas de répartition.")
    require_draft(invoice_id, invoice)
    context = invoice_context(invoice)
    with refused(f"settle_prepaid {invoice_id}", f"Facture {invoice_id} : Experio a refusé la répartition."):
        # Ticked first, as on screen (its onchange fills defaults), then the period read on the scan.
        fill_invoice(invoice_id, {"has_prepaid_expenses": True, **values}, context)
    return ok(
        f"{test_mode_note()}Facture {invoice_id} : {kind} constates d'avance sur {values['prepaid_months']} mois "
        f"à partir du {values['prepaid_start_date']} (écritures générées à la comptabilisation)."
    )
