"""Write what the scan shows over what Experio captured differently."""

from tarik.core.app import ToolError, ok, routine, step_tool, stop
from tarik.core.config import TEST_MODE
from tarik.core.conversation import instructed
from tarik.core.experio import (
    dry_run,
    fill_invoice,
    invoice_context,
    odoo_call,
    refresh,
    refused,
    require_draft_invoice,
)
from tarik.invoices.check import CAPTURED, stored_check

MULTI_TVA_RATES = {7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20}  # the amount_tva_<rate> fields Experio has

PAYMENT_METHODS = {
    "especes": "Esp",
    "cheque": "Ch",
    "virement": "Virement",
    "effet": "Effet",
    "prelevement": "Pr",
    "compensation": "Compensation",
}


def planned_changes(extracted, bad_fields, invoice):
    """What the check says to write ({field: value}): the differing header fields, the credit note and
    the multi-TVA split. ``extracted`` and ``bad_fields`` come from the check, ``invoice`` holds refund and
    amount_tax_non_recoverable."""
    changes = {
        b["field"]: extracted[b["field"]]
        for b in bad_fields
        if b["field"] in CAPTURED and extracted.get(b["field"]) is not None  # the header: tiers, account... come later
    }
    if extracted.get("is_credit_note") is True and not invoice.get("refund"):  # the model's "false" is not true
        changes["refund"] = True
    lines = [
        line for line in extracted.get("tva_lines") or [] if line.get("amount_ht") and line.get("rate") is not None
    ]
    rates = {}  # rate -> its TVA: an invoice may list several lines at one rate
    for line in lines:
        rates[int(line["rate"])] = round(rates.get(int(line["rate"]), 0) + (line.get("amount_tva") or 0), 2)
    # Split only from lines read in full that add up to the TVA read on the scan: a line not read, or read twice, never
    # becomes a written amount.
    trusted = (
        all(line.get("amount_tva") is not None for line in lines)
        and extracted.get("amount_tva") is not None
        and abs(sum(rates.values()) - extracted["amount_tva"]) <= 1
    )
    if (
        len(rates) > 1
        and trusted
        and not invoice.get("amount_tax_non_recoverable")
        and set(rates) - {0} <= MULTI_TVA_RATES
    ):
        changes["multi_tva"] = True
        changes.update({f"amount_tva_{rate}": amount for rate, amount in rates.items() if rate})
        changes["amount_tva"] = round(sum(rates.values()), 2)
    return changes


def _currency(code, changes, notes):
    """Add the currency ``code`` (ISO) the scan shows to ``changes``, or a note to ``notes`` when Experio lacks it."""
    if code == "MAD":
        return
    currency = odoo_call("res.currency", "search_read", [("name", "=", code)], fields=["id"], limit=1)
    if currency:
        changes["currency_id"] = currency[0]["id"]  # Experio fetches the rate for the invoice date itself
    else:
        notes.append(f"devise {code} inconnue dans Experio")


def _payment_method(name, invoice, changes):
    """Add the payment method ``name`` the scan shows (especes, cheque...) to ``changes``, when the invoice has none
    and Experio has it."""
    method = PAYMENT_METHODS.get((name or "").lower())
    if method and not invoice["payment_method_id"]:
        found = odoo_call(
            "exp.payment.method", "search_read", [("name", "=ilike", f"{method}%")], fields=["id"], limit=1
        )
        if found:
            changes["payment_method_id"] = found[0]["id"]


@step_tool
def correct_invoice(invoice_id: int, payment_method: str = "", client_message: str = "") -> dict:
    """Correct a draft invoice from its check: writes what check_invoice read on
    the scan over the fields Experio captured differently (amounts, number,
    date, credit note, multi-TVA, currency, payment method), then lets Experio
    rebuild its TVA lines. Call check_invoice first. A scan whose total is 0 or less is
    no invoice (a payment's receipt): stopped, nothing written. ``payment_method``: the one the
    client wrote with the invoice (especes, cheque, virement, effet, prelevement,
    compensation), over the scan's; taken only when ``client_message`` holds it."""
    fields = ["state", "company_id", "exercice_id", "refund", "amount_tax_non_recoverable", "payment_method_id"]
    invoice = require_draft_invoice(invoice_id, [*fields, "currency_id"], "etre corrigee")
    check = stored_check(invoice_id)
    if not check:
        raise ToolError(f"Facture {invoice_id} : pas encore verifiee, appelez d'abord check_invoice.")
    readable, extracted, bad_fields = check
    if not readable:
        return ok(f"Facture {invoice_id} : scan illisible, rien a corriger.")
    if extracted.get("amount_total") is not None and extracted["amount_total"] <= 0:
        # A hotel's folio after payment, a receipt: nothing owed, which Experio refuses to record anyway.
        return stop(
            f"Facture {invoice_id} : le document ne porte aucun montant a payer (total "
            f"{extracted['amount_total']:.2f}) : c'est sans doute un justificatif de paiement, pas une facture. "
            "Rien n'a ete comptabilise ; envoyez la facture elle-meme, avec ses montants."
        )
    changes = planned_changes(extracted, bad_fields, invoice)
    notes = []
    with refused(f"correct_invoice {invoice_id}", f"Facture {invoice_id} : Experio a refuse la correction."):
        _currency((extracted.get("currency") or "MAD").upper(), changes, notes)
        told = instructed(payment_method, client_message)
        _payment_method(told or extracted.get("payment_method"), invoice, changes)
        noted = f" ({'; '.join(notes)})" if notes else ""
        if not changes:
            return routine(f"Facture {invoice_id} : rien a corriger.{noted}")
        context = invoice_context(invoice)
        changed = fill_invoice(invoice_id, changes, context)
        if not changed:
            return routine(f"Facture {invoice_id} : deja a jour, rien a changer.")
        refresh(invoice_id, context)
    shown = ", ".join(f"{field} = {changed.get(field, value)}" for field, value in changes.items())
    other = sorted(set(changed) - set(changes))
    if dry_run():
        head = f"{TEST_MODE}Facture {invoice_id}, correction prevue : {shown}."
    else:
        head = f"Facture {invoice_id} : corrigee : {shown}."
    also = f" Experio a aussi mis a jour : {', '.join(other)}." if other else ""
    return ok(head + also + noted)
