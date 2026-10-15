"""Phase 14: comptabiliser -- only when Experio's own check is clear.

A straight port of tarik-master's post node, all on Experio's own methods: its refresh,
collect_validation_messages (what the Comptabiliser button checks), action_post, then the
state read back, since action_post returns nothing to trust. Posting also saves the
invoice's account, RAS and TVA flags on its tiers for next time (Experio's memorization).
"""

import time

from tarik.core.app import ok, server, stop, with_ids
from tarik.core.experio import (TEST_MODE, dry_run, experio_refusal, invoice_context, odoo_call, odoo_json, plain, refused,
                                require_invoice)

POSTED = ("post", "export")
RETRIES, PAUSE = 3, 1.0  # as tarik-master: the check can lag right after a refresh


@server.tool()
@with_ids
def post_invoice(invoice_id: int) -> dict:
    """Post (comptabiliser) a draft invoice when Experio's own check finds nothing
    blocking; otherwise list what blocks it."""
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id"])
    if invoice["state"] in POSTED:
        return ok(f"Facture {invoice_id} : deja comptabilisee.")
    if invoice["state"] != "draft":
        return stop(f"Facture {invoice_id} : en etat {invoice['state']}, elle ne peut pas etre comptabilisee.")
    context = invoice_context(invoice)
    with refused(f"post_invoice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu verifier la facture."):
        if not dry_run():
            odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)
        for attempt in range(RETRIES + 1):
            blocking = [plain(text) for _level, text in odoo_json("exp.invoice", "collect_validation_messages", [invoice_id],
                                                                   context=context) or []]
            if not blocking or attempt == RETRIES:
                break
            time.sleep(PAUSE)
    if blocking:
        return stop(f"Facture {invoice_id} : pas comptabilisee, Experio signale : " + " ; ".join(blocking))
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id} : rien ne bloque, elle serait comptabilisee.")
    try:
        # skip_due_date_detected_check: what the "date d'echeance detectee" popup's Confirmer does; over RPC the popup
        # would come back instead and nothing would post.
        odoo_json("exp.invoice", "action_post", [invoice_id], context={**context, "skip_due_date_detected_check": True})
        after = odoo_call("exp.invoice", "read", [invoice_id], fields=["state", "error_msg"], context=context)[0]
    except Exception as error:  # not refused(): a refusal here stops this invoice, with Experio's own reason
        print(f"post_invoice {invoice_id}: {error}", flush=True)
        return stop(f"Facture {invoice_id} : Experio a refuse la comptabilisation : {experio_refusal(error)}")
    if after["state"] in POSTED:
        return ok(f"Facture {invoice_id} : comptabilisee.")
    return stop(f"Facture {invoice_id} : pas comptabilisee ({plain(after['error_msg']) or 'etat ' + after['state']}).")
