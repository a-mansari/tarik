"""Stop an invoice already recorded in the dossier, before any account or TVA work.

Experio's own check (get_duplicated_invoice_ids): same number, tiers and date or year as
a posted invoice, by the cabinet's rules for purchases and sales -- plus, to catch an
invoice sent twice before either is posted, the same rule across same-number drafts. No
"TIERS - NUMERO" label is set: Experio names the writing lines from each cabinet's own label
format, and the label field would override it.
"""

from engine.tools.core.app import log, ok, routine, step_tool, stop
from engine.tools.core.experio import invoice_context, odoo_call, odoo_json, plain, refused, require_invoice

FIELDS = ["company_id", "exercice_id", "invoice_number", "invoice_date", "amount_total", "partner_id", "type"]


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


def without_tiers_twin(invoice, context):
    """A stop when an older invoice of the dossier, not cancelled, is this one with no tiers to compare by: the
    same type, number (spaces aside), date and total, one of them without its tiers yet (Experio's check needs
    both). An invoice waiting on its supplier question is still caught, and one asked about after it too."""
    if not invoice.get("invoice_number") or invoice.get("amount_total") is None:
        return None
    domain = [
        ("company_id", "=", invoice["company_id"][0]),
        ("type", "=", invoice["type"]),
        ("id", "<", invoice["id"]),
        ("state", "!=", "cancel"),
        ("invoice_number", "ilike", invoice["invoice_number"].strip()),
    ]
    if invoice["partner_id"]:
        domain.append(("partner_id", "=", False))
    fields = ["invoice_number", "invoice_date", "amount_total"]
    found = odoo_call("exp.invoice", "search_read", domain, fields=fields, context=context)
    number = invoice["invoice_number"].replace(" ", "")
    twins = sorted(
        other["id"]
        for other in found
        if (other.get("invoice_number") or "").replace(" ", "") == number
        and other.get("invoice_date") == invoice.get("invoice_date")
        and other.get("amount_total") == invoice["amount_total"]
    )
    if not twins:
        return None
    return stop(
        f"Facture {invoice['id']} : doublon de la facture {twins[0]} (même numéro, date et montant), non traitée.",
        duplicate=True,
    )


@step_tool
def check_duplicate(invoice_id: int) -> dict:
    """Check whether this invoice is already recorded in its dossier (same number,
    tiers and date; or same number, date and total when one has no tiers yet).
    Call it once the tiers is set. A duplicate must not be processed any further."""
    invoice = require_invoice(invoice_id, FIELDS)
    twin = without_tiers_twin(invoice, invoice_context(invoice))
    if twin:
        return twin
    if not invoice["invoice_number"] or not invoice["partner_id"]:
        return ok(
            f"Facture {invoice_id} : numéro ou tiers manquant, "
            "la vérification des doublons attend qu'ils soient renseignés."
        )
    context = invoice_context(invoice)
    with refused(f"check_duplicate {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier les doublons."):
        # 1. Against posted invoices: Experio's check on this invoice alone.
        if invoice_id in (odoo_json("exp.invoice", "get_duplicated_invoice_ids", [invoice_id], context=context) or []):
            message = odoo_json("exp.invoice", "get_duplicated_invoice_msg", [invoice_id], context=context) or ""
            log(f"check_duplicate {invoice_id}: {plain(message)}")  # Experio's long wording: for the team
            message = (
                f"déjà enregistrée (n° {invoice['invoice_number']}, {invoice['partner_id'][1].split(' - ')[0]}, "
                f"{invoice['invoice_date'] or 'sans date'})"
            )
        else:
            # 2. Against the dossier's other drafts with the same number and tiers, checked together so Experio's
            # rule (date or year) decides. Its "original" depends on SQL order, so the oldest draft is kept instead.
            batch = _same_number_drafts(invoice, context)
            if len(batch) == 1 or not odoo_json("exp.invoice", "get_duplicated_invoice_ids", batch, context=context):
                return routine(f"Facture {invoice_id} : pas de doublon dans le dossier.")
            if invoice_id == batch[0]:
                return routine(
                    f"Facture {invoice_id} : pas de doublon, c'est la première saisie ; les brouillons "
                    f"{', '.join(map(str, batch[1:]))} en sont des doublons."
                )
            message = f"doublon du brouillon {batch[0]}, saisi avant"
    return stop(f"Facture {invoice_id} : {message}, non traitée.", duplicate=True)
