"""Phase 4: write what the scan shows over what Experio captured differently."""

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import with_ids, ok, server
from tarik.core.experio import DRY_RUN, POSTGRES, fill_invoice, invoice_context, odoo_call, odoo_json, require_invoice


# Header fields the correction may write (tarik-master odoo.WRITABLE); tiers, account, journal... are later steps.
WRITABLE = {"invoice_number", "invoice_date", "amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement",
            "amount_guarantee_holdback", "amount_prorata", "amount_insurance", "amount_ts", "amount_tsp",
            "advance_amount", "amount_total"}

MULTI_TVA_RATES = {7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20}  # the amount_tva_<rate> fields Experio has

PAYMENT_METHODS = {"especes": "Esp", "cheque": "Ch", "virement": "Virement", "effet": "Effet",
                   "prelevement": "Pr", "compensation": "Compensation"}

def planned_changes(extracted, bad_fields, invoice):
    """What the check says to write: the differing header fields, the credit note and
    the multi-TVA split (tarik-master orchestrator.correct + odoo.apply_variants)."""
    changes = {b["field"]: extracted[b["field"]] for b in bad_fields
               if b["field"] in WRITABLE and extracted.get(b["field"]) is not None}
    if extracted.get("is_credit_note") and not invoice.get("refund"):
        changes["refund"] = True
    rates = {int(line["rate"]): line["amount_tva"] for line in extracted.get("tva_lines") or []
             if line.get("amount_ht") and line.get("rate") is not None}
    if len(rates) > 1 and not invoice.get("amount_tax_non_recoverable") and set(rates) - {0} <= MULTI_TVA_RATES:
        changes["multi_tva"] = True
        changes.update({f"amount_tva_{rate}": amount for rate, amount in rates.items() if rate})
        changes["amount_tva"] = round(sum(rates.values()), 2)
    return changes

@server.tool()
@with_ids
def correct_invoice(invoice_id: int) -> dict:
    """Correct a draft invoice from its check: writes what check_invoice read on
    the scan over the fields Experio captured differently (amounts, number,
    date, credit note, multi-TVA, currency, payment method), then lets Experio
    rebuild its TVA lines. Call check_invoice first."""
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id", "refund", "amount_tax_non_recoverable",
                                           "payment_method_id", "currency_id"])
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut etre corrigee.")
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select readable, extracted, bad_fields from tarik_checks where invoice_id = %s", (invoice_id,)).fetchone()
    if not row:
        raise ToolError(f"Facture {invoice_id} : pas encore verifiee, appelez d'abord check_invoice.")
    readable, extracted, bad_fields = row
    if not readable:
        return ok(f"Facture {invoice_id} : scan illisible, rien a corriger.")
    changes = planned_changes(extracted, bad_fields, invoice)
    notes = []
    try:
        code = (extracted.get("currency") or "MAD").upper()
        if code != "MAD":
            currency = odoo_call("res.currency", "search_read", [("name", "=", code)], fields=["id"], limit=1)
            if currency:
                changes["currency_id"] = currency[0]["id"]  # Experio fetches the rate for the invoice date itself
            else:
                notes.append(f"devise {code} inconnue dans Experio")
        method = PAYMENT_METHODS.get((extracted.get("payment_method") or "").lower())
        if method and not invoice["payment_method_id"]:
            found = odoo_call("exp.payment.method", "search_read", [("name", "=ilike", f"{method}%")], fields=["id"], limit=1)
            if found:
                changes["payment_method_id"] = found[0]["id"]
        if not changes:
            return ok(f"Facture {invoice_id} : rien a corriger." + (f" ({'; '.join(notes)})" if notes else ""))
        context = invoice_context(invoice)
        changed = fill_invoice(invoice_id, changes, context)
        if not changed:
            return ok(f"Facture {invoice_id} : deja a jour, rien a changer.")
        if not DRY_RUN:
            odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)  # Experio's own "Actualiser"; returns None, hence JSON-RPC
    except Exception as error:
        print(f"correct_invoice {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio a refuse la correction.") from None
    shown = ", ".join(f"{field} = {changed.get(field, value)}" for field, value in changes.items())
    other = sorted(set(changed) - set(changes))
    head = (f"MODE TEST : aucune modification enregistree dans Experio. Facture {invoice_id}, correction prevue : {shown}."
            if DRY_RUN else f"Facture {invoice_id} : corrigee : {shown}.")
    return ok(head + (f" Experio a aussi mis a jour : {', '.join(other)}." if other else "") + (f" ({'; '.join(notes)})" if notes else ""))
