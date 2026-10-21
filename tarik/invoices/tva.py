"""TVA -- recoverable or not, the cabinet's prorata, and the tax on each line.

Experio does most of it: picking the tiers
in the form applies its memorized non-recoverable and prorata flags, and its refresh picks
the tax for each rate from the dossier's tax settings (or says which setting is missing).
Left here: the question on the usual non-recoverable accounts, and the cabinet's prorata.
"""

from tarik.core.app import ToolError, ok, question, routine, step_tool
from tarik.core.config import TEST_MODE
from tarik.core.conversation import kept_yes_no, yes_no
from tarik.core.experio import (
    account_code,
    dry_run,
    fill_invoice,
    invoice_context,
    odoo_call,
    refresh,
    refused,
    require_draft_invoice,
    test_mode_note,
)
from tarik.invoices.classify import side_of

# CGI art. 106: a confirmer par le comptable avant usage en production.
NON_RECOVERABLE_ACCOUNTS = ("6142", "6143", "61251", "2340")  # travel, receptions, fuel, passenger cars


def _non_recoverable(invoice, answer):
    """For a purchase with TVA not yet non-recoverable: the question to ask the client (a question result), or the
    changes their ``answer`` makes ({} when none). Returns (question or None, changes)."""
    told = yes_no(answer) if answer else None
    if answer and told is None:
        return (
            question(f"Facture {invoice['id']} : reponse non comprise, la TVA est-elle non recuperable ? oui / non"),
            {},
        )
    if told is None and account_code(invoice).startswith(NON_RECOVERABLE_ACCOUNTS):
        tier = (invoice["partner_id"] or [0, "ce fournisseur"])[1]
        asked = f"Facture de {tier} : la TVA est-elle non recuperable (voyage, carburant, vehicule de tourisme) ? "
        return question(asked + "oui / non"), {}
    return None, ({"amount_tax_non_recoverable": True, "multi_tva": False} if told else {})


def _tax_lines(invoice_id, context):
    """The invoice's TVA lines once Experio rebuilt them, as "<HT> HT a <rate> % (<tax>)" for each untaxed line."""
    line_ids = odoo_call("exp.invoice", "read", [invoice_id], fields=["tva_line_ids"], context=context)[0][
        "tva_line_ids"
    ]
    fields = ["line_type", "tax_id", "tax_rate", "amount_ht", "amount_tva"]
    lines = odoo_call("exp.invoice.line", "read", line_ids, fields=fields, context=context) if line_ids else []
    return [
        f"{line['amount_ht']} HT a {line['tax_rate']} % ({line['tax_id'][1] if line['tax_id'] else 'sans taxe'})"
        for line in lines
        if line["line_type"] == "untaxed"
    ]


@step_tool
def settle_tva(invoice_id: int, answer: str = "") -> dict:
    """Settle a draft invoice's TVA once its account is set: recoverable or not,
    the cabinet's prorata, and the tax Experio puts on each line. When the answer
    is a question (non-recoverable TVA?), ask the client and call again with
    ``answer`` "oui" (non-recoverable) or "non", as the agent read their reply."""
    fields = ["state", "type", "company_id", "exercice_id", "partner_id", "account_id", "amount_tva"]
    invoice = require_draft_invoice(invoice_id, [*fields, "amount_tax_non_recoverable", "tax_prorata", "tva_line_ids"])
    if not invoice["account_id"]:
        if dry_run():  # the account step only simulated its write
            return ok(f"{TEST_MODE}Facture {invoice_id} : la TVA sera reglee une fois le compte enregistre.")
        raise ToolError(
            f"Facture {invoice_id} : pas encore de compte, appelez d'abord assign_account (la TVA en depend)."
        )
    purchase = side_of(invoice) == "purchase"
    context = invoice_context(invoice)
    changes, notes = {}, []
    if purchase and not invoice["amount_tax_non_recoverable"] and invoice["amount_tva"]:
        answer, _ = kept_yes_no(invoice_id, "settle_tva", answer)  # asked before: its answer, not the question again
        asked, changes = _non_recoverable(invoice, answer)
        if asked:
            return asked
    non_recoverable = invoice["amount_tax_non_recoverable"] or changes.get("amount_tax_non_recoverable")
    with refused(f"settle_tva {invoice_id}", f"Facture {invoice_id} : Experio a refuse la mise a jour de la TVA."):
        if purchase and not non_recoverable and invoice["amount_tva"] and not invoice["tax_prorata"]:
            fields = ["use_prorata", "prorata_rate"]
            company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=fields)[0]
            if company["use_prorata"]:  # the dossier uses a TVA prorata
                changes["tax_prorata"] = True  # Experio's onchange applies the cabinet's rate
                notes.append(f"prorata de TVA {company['prorata_rate']} %")
        if changes:
            fill_invoice(invoice_id, changes, context)
            if changes.get("amount_tax_non_recoverable"):
                notes.append("TVA non recuperable")  # Experio saves it on the supplier when the invoice is posted
        refresh(invoice_id, context)  # Experio picks the tax for each rate
        taxed = _tax_lines(invoice_id, context)
    # Only the TVA is said here: what else Experio refuses on the invoice is configure_cabinet's and post_invoice's.
    body = f"{test_mode_note()}Facture {invoice_id} : TVA {'non recuperable' if non_recoverable else 'recuperable'}"
    body += (f", {', '.join(notes)}" if notes and not non_recoverable else "") + ". "
    body += ("Lignes : " + "; ".join(taxed) + ".") if taxed else "Pas encore de ligne de TVA."
    told = non_recoverable or notes  # what the client should know: else routine
    return ok(body) if told else routine(body)
