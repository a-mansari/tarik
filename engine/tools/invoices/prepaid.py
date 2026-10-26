"""Charges (or produits) constatées d'avance: an invoice billing a period over several months.

Bluejay does not detect the period; check_invoice reads it on the scan. When it spans several months, the client is
asked whether to spread the charge. Experio does the rest: ticking it in the form fills and validates the period, and
posting generates the spreading entries with the dossier's prepaid account and journal.
"""

from engine.tools.core.app import ok, question, routine, step_tool, stop
from engine.tools.core.conversation import kept_yes_no
from engine.tools.core.experio import (
    dry_run,
    fill_invoice,
    invoice_context,
    odoo_call,
    refused,
    require_draft,
    require_invoice,
    test_mode_note,
)
from engine.tools.invoices.check import stored_check
from engine.tools.invoices.configuration import save_settings


def prepaid_period(start, end):
    """Return the billed period from ``start`` to ``end`` (YYYY-MM-DD) as Experio's prepaid fields.

    Returns {prepaid_start_date, prepaid_end_date, prepaid_months} when the period spans two calendar months or more,
    else None (also for a missing or malformed date, or ``end`` not after ``start``).
    """
    if not (start and end and len(start) == len(end) == 10 and start < end):
        return None
    months = month_number(end) - month_number(start) + 1
    if months < 2:
        return None
    return {"prepaid_start_date": start, "prepaid_end_date": end, "prepaid_months": months}


def month_number(day):
    """Return ``day`` (YYYY-MM-DD) as a count of months (year * 12 + month), so two days can be subtracted."""
    return int(day[:4]) * 12 + int(day[5:7])  # its year, then its month


@step_tool
def settle_prepaid(invoice_id: int, answer: str = "") -> dict:
    """Ask whether to spread a draft invoice billing a period over several months, then spread it on « oui ».

    The period comes from the stored check of the scan. Spreading means charges (or produits) constatées d'avance:
    the box ticked and the period filled through Experio's form, the dossier's prepaid journal set first when it has
    none. Call again with ``answer`` "oui" or "non", as the agent read the client's reply. An invoice already spread
    only gets its missing journal set.
    """
    invoice = require_invoice(
        invoice_id, ["state", "type", "company_id", "exercice_id", "invoice_number", "has_prepaid_expenses"]
    )
    if invoice["has_prepaid_expenses"]:
        # Spread before its journal was set: set now, or Experio refuses its posting.
        journal = invoice["state"] == "draft" and prepaid_journal(invoice["company_id"][0], invoice_context(invoice))
        if journal:
            return ok(f"{test_mode_note()}Facture {invoice_id} : déjà répartie en constatée d'avance{journal}.")
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
        journal = prepaid_journal(invoice["company_id"][0], context)
        if journal is None:
            return stop(
                f"Facture {invoice_id} : le dossier n'a pas de journal d'opérations diverses pour les {kind} "
                "constatées d'avance : à choisir dans Configuration, Général, Charges/produits constatés d'avance."
            )
        # Ticked first, as on screen (its onchange fills defaults), then the period read on the scan.
        fill_invoice(invoice_id, {"has_prepaid_expenses": True, **values}, context)
    return ok(
        f"{test_mode_note()}Facture {invoice_id} : {kind} constates d'avance sur {values['prepaid_months']} mois "
        f"à partir du {values['prepaid_start_date']} (écritures générées à la comptabilisation){journal}."
    )


def prepaid_journal(company_id, context):
    """Make sure the dossier has a prepaid journal, setting one when missing, and return a note for the reply.

    Returns:
        "" when the dossier already has a prepaid journal; a note (starting " ; journal ...") naming the journal set,
        or in test mode the one that would be set; None when no journal could be set (the dossier has no general
        journal of category normal, or the cabinet's rights refused the settings write).

    Experio leaves prepaid_journal_id empty: the field has no default, so Experio's automatic detection
    (res.company auto_detect_settings) skips it, and get_prepaid_journal_id refuses the posting without it. The journal
    is chosen with the rule Experio uses for its exchange-difference journal default: the first general journal of
    category normal, which excludes « A nouveaux » (_default_currency_exchange_journal_id, exp_account
    res_company.py:52).
    """
    (company,) = odoo_call("res.company", "read", [company_id], fields=["prepaid_journal_id"], context=context)
    if company["prepaid_journal_id"]:
        return ""
    found = odoo_call(
        "exp.journal",
        "search_read",
        [("company_id", "=", company_id), ("type", "=", "general"), ("journal_category", "=", "normal")],
        fields=["name"],
        limit=1,
        context=context,
    )
    if not found:
        return None
    if dry_run():
        return f" ; journal `{found[0]['name']}` à paramétrer dans le dossier pour la répartition"
    if not save_settings(company_id, {"prepaid_journal_id": found[0]["id"]}, context):
        return None  # the cabinet's rights refused it: Configuration is where it is chosen
    return f" ; journal `{found[0]['name']}` paramétré dans le dossier pour la répartition"
