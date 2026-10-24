"""A posted credit note (avoir) applied to the invoice it cancels, as the cabinet does from Experio's « Crédits /
Débits en attente » block.

Experio lists, on the posted credit note, the open lines of the same tiers and account it may be applied to
(exp.move _compute_payments_widget_to_reconcile_info: each with its entry's number and what is left on it), and
applies one with js_assign_outstanding_line (a reconciliation, undone from the same block). Tarik applies it when the
choice is plain: the invoice the credit note names on its scan, else the one invoice of that tiers left owing exactly
the credit note's amount. Anything else is listed for the cabinet.
"""

from engine.tools.core.app import ok, routine, step_tool
from engine.tools.core.config import POSTED, TEST_MODE
from engine.tools.core.experio import dry_run, in_dossier, odoo_call, odoo_json, refused, require_invoice
from engine.tools.core.words import normalized
from engine.tools.invoices.check import stored_check

FIELDS = ["state", "refund", "company_id", "partner_id", "move_id", "amount_residual", "invoice_number"]
WIDGET = "invoice_outstanding_credits_debits_widget"


def plain(number):
    """An invoice number as compared: lowercase letters and digits only ("FA 2026-01" is "fa202601")."""
    return normalized(str(number or "")).replace(" ", "")


def invoice_lines(candidates, partner_id):
    """Of the widget's ``candidates``, those that are an invoice of the tiers (not a credit note, a payment nor an
    opening balance), by their entry."""
    moves = [line["move_id"] for line in candidates]
    domain = [("move_id", "in", moves), ("partner_id", "=", partner_id), ("refund", "=", False)]
    found = {i["move_id"][0] for i in odoo_call("exp.invoice", "search_read", domain, fields=["move_id"])}
    return [line for line in candidates if line["move_id"] in found]


def chosen(candidates, named, amount):
    """The line to apply the credit note to, and why: the invoice it names (``named``, read on its scan), else the one
    invoice left owing exactly its ``amount``; (None, "") when it is no plain choice."""
    if named:
        by_name = [line for line in candidates if plain(line["journal_name"]) == plain(named)]
        if len(by_name) == 1:
            return by_name[0], f"la facture {by_name[0]['journal_name']} qu'il cite"
    same = [line for line in candidates if round(line["amount"], 2) == round(amount, 2)]
    if len(same) == 1:
        return same[0], f"la seule facture de ce tiers restant due du même montant ({amount:.2f})"
    return None, ""


@step_tool
def apply_credit_note(invoice_id: int) -> dict:
    """Apply a posted credit note to the invoice it cancels, with Experio's own « Crédits en attente »: the invoice
    its scan names, else the one invoice of that tiers left owing exactly its amount; otherwise the candidates are
    listed for the cabinet. Nothing to do for an invoice, a draft, or a credit note already used up."""
    invoice = require_invoice(invoice_id, FIELDS)
    if not invoice["refund"] or invoice["state"] not in POSTED:
        return routine(f"Facture {invoice_id} : pas d'avoir comptabilisé à imputer.")
    amount = round(invoice["amount_residual"] or 0, 2)
    if not amount or not invoice["partner_id"]:
        return routine(f"Facture {invoice_id} : avoir déjà imputé.")
    context = in_dossier(invoice["company_id"][0])
    with refused(f"apply_credit_note {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu lister les factures."):
        widget = odoo_call("exp.invoice", "read", [invoice_id], fields=[WIDGET], context=context)[0][WIDGET] or {}
        candidates = invoice_lines(widget.get("content") or [], invoice["partner_id"][0])
    if not candidates:
        return routine(f"Facture {invoice_id} : avoir sans facture de ce tiers à imputer, laissé tel quel.")
    check = stored_check(invoice_id)
    named = check[1].get("credit_note_of") if check else None
    line, why = chosen(candidates, named, amount)
    if not line:
        listed = ", ".join(f"{c['journal_name']} ({c['amount']:.2f})" for c in candidates[:5])
        return ok(
            f"Facture {invoice_id} : avoir de {amount:.2f} à imputer par le cabinet dans Experio (Crédits en attente), "
            f"la facture n'étant pas certaine : {listed}."
        )
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id} : l'avoir serait imputé sur {why}.")
    with refused(
        f"apply_credit_note {invoice_id}", f"Facture {invoice_id} : Experio a refusé l'imputation de l'avoir."
    ):
        odoo_json("exp.move", "js_assign_outstanding_line", [invoice["move_id"][0]], line["id"], context=context)
        left = odoo_call("exp.invoice", "read", [invoice_id], fields=["amount_residual"], context=context)[0]
    rest = round(left["amount_residual"] or 0, 2)
    tail = f", reste {rest:.2f} à imputer" if rest else ""
    return ok(f"Facture {invoice_id} : avoir imputé sur {why}{tail} (annulable dans Experio, Crédits en attente).")
