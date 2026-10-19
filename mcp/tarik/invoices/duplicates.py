"""Stop an invoice already recorded in the dossier, before any account or TVA work.

Experio's own check (get_duplicated_invoice_ids): same number, tiers and date or year as
a posted invoice, by the cabinet's rules for purchases and sales -- plus, to catch an
invoice sent twice before either is posted, the same rule across same-number drafts. No
"TIERS - NUMERO" label is set: Experio names the writing lines from each cabinet's own label
format, and the label field would override it.
"""

from tarik.core.app import ok, routine, server, stop, with_ids
from tarik.core.experio import invoice_context, odoo_call, odoo_json, plain, refused, require_invoice


def _same_number_drafts(invoice, context):
    """The ids of the dossier's drafts with the invoice's type, tiers and number (spaces aside), its own included,
    sorted: oldest first."""
    number = invoice["invoice_number"].replace(" ", "")
    domain = [
        ("company_id", "=", invoice["company_id"][0]),
        ("type", "=", invoice["type"]),
        ("id", "!=", invoice["id"]),
        ("partner_id", "=", invoice["partner_id"][0]),
        ("state", "in", ["pending", "draft"]),  # a cancelled one is no duplicate (Experio counts every id given)
        ("invoice_number", "ilike", invoice["invoice_number"].strip()),
    ]
    drafts = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number"], context=context)
    same = [d["id"] for d in drafts if (d["invoice_number"] or "").replace(" ", "") == number]
    return sorted([invoice["id"]] + same)


@server.tool()
@with_ids
def check_duplicate(invoice_id: int) -> dict:
    """Check whether this invoice is already recorded in its dossier (same number,
    tiers and date). Call it once the tiers is set. A duplicate must not be
    processed any further."""
    invoice = require_invoice(invoice_id, ["company_id", "exercice_id", "invoice_number", "partner_id", "type"])
    if not invoice["invoice_number"] or not invoice["partner_id"]:
        return ok(
            f"Facture {invoice_id} : numero ou tiers manquant, "
            "la verification des doublons attend qu'ils soient renseignes."
        )
    context = invoice_context(invoice)
    with refused(f"check_duplicate {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu verifier les doublons."):
        # 1. Against posted invoices: Experio's check on this invoice alone.
        if invoice_id in (odoo_json("exp.invoice", "get_duplicated_invoice_ids", [invoice_id], context=context) or []):
            message = odoo_json("exp.invoice", "get_duplicated_invoice_msg", [invoice_id], context=context) or ""
        else:
            # 2. Against the dossier's other drafts with the same number and tiers, checked together so Experio's
            # rule (date or year) decides. Its "original" depends on SQL order, so the oldest draft is kept instead.
            batch = _same_number_drafts(invoice, context)
            if len(batch) == 1 or not odoo_json("exp.invoice", "get_duplicated_invoice_ids", batch, context=context):
                return routine(f"Facture {invoice_id} : pas de doublon dans le dossier.")
            if invoice_id == batch[0]:
                return ok(
                    f"Facture {invoice_id} : pas de doublon, c'est la premiere saisie ; les brouillons "
                    f"{', '.join(map(str, batch[1:]))} en sont des doublons."
                )
            message = f"Meme facture que le brouillon {batch[0]}, saisi avant."
    return stop(
        f"Facture {invoice_id} : DOUBLON, deja enregistree dans le dossier. {plain(message)} Ne pas la traiter."
    )
