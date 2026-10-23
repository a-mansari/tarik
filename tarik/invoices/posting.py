"""Comptabiliser -- only when Experio's own check is clear.

All on Experio's own methods: its refresh,
collect_validation_messages (what the Comptabiliser button checks), action_post, then the
state read back, since action_post returns nothing to trust. Posting also saves the
invoice's account, RAS and TVA flags on its tiers for next time (Experio's memorization).
"""

import time

from tarik.core.app import log, ok, routine, step_tool, stop
from tarik.core.config import POSTED, TEST_MODE
from tarik.core.experio import (
    blocking,
    dry_run,
    experio_refusal,
    invoice_context,
    odoo_call,
    odoo_json,
    plain,
    refresh,
    refused,
    require_invoice,
)

RETRIES, PAUSE = 3, 1.0  # the check can lag right after a refresh
ACCOUNT = {
    "purchase": ("third_party_supplier_account", "fournisseur"),
    "sale": ("third_party_customer_account", "client"),
}


def missing_compte_tiers(invoice):
    """The role ("fournisseur" or "client") whose Compte Tiers the invoice's tiers lacks, else None. Experio posts
    without it unless the dossier turned on its optional block, and a posting without one cannot be exported."""
    field, role = ACCOUNT.get(invoice["type"], (None, None))
    if not field or not invoice["partner_id"]:
        return None
    company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=["ignore_third_party_account"])[0]
    if company["ignore_third_party_account"]:
        return None
    partner = odoo_call("exp.partner", "read", [invoice["partner_id"][0]], fields=[field])[0]
    return None if partner[field] else role


def _blocking(invoice_id, context):
    """What Experio's own check (the Comptabiliser button's) says blocks the invoice ([str], plain text), asked
    again up to RETRIES times while it says something."""
    for attempt in range(RETRIES + 1):
        found = blocking(invoice_id, context)
        if not found or attempt == RETRIES:
            return found
        time.sleep(PAUSE)


@step_tool
def post_invoice(invoice_id: int) -> dict:
    """Post (comptabiliser) a draft invoice when Experio's own check finds nothing
    blocking; otherwise list what blocks it."""
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id", "type", "partner_id"])
    if invoice["state"] in POSTED:
        return routine(f"Facture {invoice_id} : déjà comptabilisée.")
    if invoice["state"] != "draft":
        return stop(f"Facture {invoice_id} : en état {invoice['state']}, elle ne peut pas être comptabilisée.")
    role = missing_compte_tiers(invoice)
    if role:
        return stop(
            f"Facture {invoice_id} : pas comptabilisée, son tiers n'a pas de Compte Tiers {role} : le cabinet doit "
            "l'attribuer dans Experio (fiche du tiers), puis me dire de la comptabiliser."
        )
    context = invoice_context(invoice)
    with refused(f"post_invoice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier la facture."):
        refresh(invoice_id, context)
        found = _blocking(invoice_id, context)
    if found:
        return stop(f"Facture {invoice_id} : pas comptabilisée, Experio signale : " + " ; ".join(found))
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id} : rien ne bloque, elle serait comptabilisée.")
    try:
        # skip_due_date_detected_check: what the "date d'echeance detectee" popup's Confirmer does; over RPC the popup
        # would come back instead and nothing would post.
        odoo_json("exp.invoice", "action_post", [invoice_id], context={**context, "skip_due_date_detected_check": True})
        after = odoo_call("exp.invoice", "read", [invoice_id], fields=["state", "error_msg"], context=context)[0]
    except Exception as error:  # not refused(): a refusal here stops this invoice, with Experio's own reason
        log(f"post_invoice {invoice_id}: {error}")
        return stop(f"Facture {invoice_id} : Experio a refusé la comptabilisation : {experio_refusal(error)}")
    if after["state"] in POSTED:
        return ok(f"Facture {invoice_id} : comptabilisée.")
    return stop(f"Facture {invoice_id} : pas comptabilisée ({plain(after['error_msg']) or 'état ' + after['state']}).")
