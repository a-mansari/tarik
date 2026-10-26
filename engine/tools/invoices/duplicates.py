"""Stop an invoice already recorded in the dossier, before any account or TVA work.

Experio's own check (exp.invoice get_duplicated_invoice_ids / get_duplicated_invoice_ids_impl) compares an invoice
with the posted ones of the same type and credit-note flag: for a purchase, same number (spaces aside) and tiers,
plus the date when the dossier says so (purchase_invoice_duplicate_check_date); for a sale, same number, plus the
year when the dossier says so (check_duplicate_with_year), whatever the customer. To catch an invoice sent twice
before either is posted, Tarik also runs it on the same-number drafts together. No "TIERS - NUMERO" label is set:
Experio names the writing lines from each cabinet's own label format, and the label field would override it.
"""

from engine.tools.core.app import log, ok, routine, step_tool, stop
from engine.tools.core.experio import invoice_context, odoo_call, odoo_json, plain, refused, require_invoice

FIELDS = ["company_id", "exercice_id", "invoice_number", "invoice_date", "amount_total", "partner_id", "type", "refund"]


def _same_number_drafts(invoice, context):
    """Return the ids of the dossier's drafts with the invoice's type and number (spaces aside), and its tiers for a
    purchase (Experio's sale rule ignores the customer). The invoice's own id is included; sorted, oldest first."""
    number = invoice["invoice_number"].replace(" ", "")
    domain = [
        ("company_id", "=", invoice["company_id"][0]),
        ("type", "=", invoice["type"]),
        ("id", "!=", invoice["id"]),
        ("state", "in", ["pending", "draft"]),  # a cancelled one is no duplicate (Experio counts every id given)
        ("invoice_number", "ilike", invoice["invoice_number"].strip()),
    ]
    if invoice["type"] == "purchase":
        domain.append(("partner_id", "=", invoice["partner_id"][0]))
    drafts = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number"], context=context)
    same = [d["id"] for d in drafts if (d["invoice_number"] or "").replace(" ", "") == number]
    return sorted([invoice["id"]] + same)


def without_tiers_twin(invoice, context):
    """Return a stop result when an older, not cancelled invoice of the dossier is this one with no tiers to
    compare by, else None.

    Same type, credit-note flag, number (spaces aside), date and total, with one of the two still without its tiers:
    Experio's check needs both tiers. So an invoice waiting on its supplier question is still caught, and so is one
    asked about after it."""
    if not invoice.get("invoice_number") or invoice.get("amount_total") is None:
        return None
    domain = [
        ("company_id", "=", invoice["company_id"][0]),
        ("type", "=", invoice["type"]),
        ("id", "<", invoice["id"]),
        ("refund", "=", bool(invoice.get("refund"))),  # an invoice and its credit note are no twins (Experio's key)
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
    """Check whether this invoice is already recorded in its dossier, by Experio's rule for its side. Call it once
    the tiers is set.

    A purchase: same number and tiers (and date when the dossier says so), or same number, date and total when one
    has no tiers yet. A sale: same number (and year when the dossier says so). Compared with the posted invoices,
    then with the same-number drafts, where the oldest is the original. A duplicate gets a stop result
    (duplicate=True) and must not be processed any further."""
    invoice = require_invoice(invoice_id, FIELDS)
    sale = invoice["type"] == "sale"  # Experio's sale rule has no customer: nothing to wait for but the number
    twin = None if sale else without_tiers_twin(invoice, invoice_context(invoice))
    if twin:
        return twin
    if not invoice["invoice_number"] or not (invoice["partner_id"] or sale):
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
            tier = f"`{invoice['partner_id'][1].split(' - ')[0]}`" if invoice["partner_id"] else "client non renseigné"
            message = (
                f"déjà enregistrée (n° {invoice['invoice_number']}, {tier}, {invoice['invoice_date'] or 'sans date'})"
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
