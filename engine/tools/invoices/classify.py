"""Decide whether a draft is a purchase, a sale, or not an invoice at all, and set its type in Experio."""

import re

from engine.tools.core.app import ToolError, log, ok, routine, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.experio import (
    fill_invoice,
    has_scan,
    invoice_context,
    odoo_call,
    refused,
    require_draft,
    require_invoice,
    still_reading,
    test_mode_note,
)
from engine.tools.core.model import READER, ask_model, scan_pages, tool_prompt
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized

CATEGORY_TO_TYPE = {"achat": "purchase", "vente": "sale"}
# How a client says which side their invoices are on (matched on normalized words).
SIDE_WORDS = {"achat": r"achats?|fournisseurs?|purchases?", "vente": r"ventes?|sales?|factures? (?:de )?clients?"}
SIDES = {"achat": {"achat"}, "vente": {"vente"}, "mixte": {"achat", "vente"}}


def classify_pages(pages, what, dossier=""):
    """Ask the model what a document's ``pages`` are. Returns (category, reason in French): category is achat,
    vente, rb (bank statement) or autre (neither: a contract, a schedule, a screenshot).

    Args:
        what: the step's name in logs and errors.
        dossier: the dossier's name when known; it tells the seller from the buyer.

    Raises ToolError when the model answers another category."""
    client = f"The cabinet's client here is « {dossier} ». " if dossier else ""
    result = ask_model(
        tool_prompt("classify-document"),
        pages,
        client
        + 'Answer with only a JSON object: {"category": "achat"|"vente"|"rb"|"autre", "reason": "one French sentence"}',
        what,
    )
    if result.get("category") not in ("achat", "vente", "rb", "autre"):
        log(f"{what}: unexpected category {result.get('category')!r}")
        raise ToolError(
            f"{what} : {READER} n'a pas su dire si c'est une facture d'achat, de vente ou un relevé bancaire ; "
            "précisez-le dans votre message."
        )
    return result["category"], str(result.get("reason") or "")


def side_of(invoice):
    """Return the side of ``invoice`` (dict with id and type), "purchase" or "sale": from Tarik's classification
    when there is one, else Experio's type. Raises ToolError for a bank statement, another document, or a type
    that is neither side."""
    with cabinet_db() as db:
        row = db.execute(Q.classification_of_invoice, (invoice["id"],)).fetchone()
    if row and row[0] in ("rb", "autre"):
        raise ToolError(f"Facture {invoice['id']} : ce n'est pas une facture.")
    side = CATEGORY_TO_TYPE[row[0]] if row else invoice["type"]
    if side not in ("purchase", "sale"):
        raise ToolError(f"Facture {invoice['id']} : ni achat ni vente, classez-la d'abord avec classify_invoice.")
    return side


def client_side(invoice_type, words):
    """Return ``invoice_type`` (achat, vente, or mixte for both) when the client's ``words`` say that side
    themselves, else None: a model may not choose the side of the client's invoices."""
    # Each side's words, as whole words.
    written = {
        side for side, pattern in SIDE_WORDS.items() if re.search(rf"\b(?:{pattern})\b", normalized(words or ""))
    }
    return invoice_type if invoice_type in SIDES and SIDES[invoice_type] <= written else None


def side_by_ice(invoice, company):
    """Return "vente" when the issuer Experio read carries the dossier's ICE or IF, "achat" when the buyer read does,
    else None. ``company``: the dossier's ice and vat (its IF).

    The same test as Experio's warning that an invoice may be on the wrong side
    (exp.invoice validate_warning_invalid_ice_and_if)."""
    for number, field in (("ice", "ice"), ("if", "vat")):
        own = company[field]
        if own and invoice[f"detected_supplier_{number}"] == own:
            return "vente"
        if own and invoice[f"detected_customer_{number}"] == own:
            return "achat"
    return None


def _kept_classification(invoice_id, invoice_type):
    """Return the (category, reason) already stored for the invoice, or None when there is none or the client now
    gives another side (``invoice_type``): a draft taken up again keeps its side, the model's second look must
    not flip it."""
    with cabinet_db() as db:
        row = db.execute(Q.classification_of_invoice, (invoice_id,)).fetchone()
    return row if row and invoice_type in ("", row[0]) else None


def _save_classification(invoice_id, category, reason):
    """Store the invoice's ``category`` and its ``reason`` in tarik_classifications, replacing any earlier one."""
    with cabinet_db() as db:
        db.execute(
            Q.upsert_classification,
            (invoice_id, category, reason),
        )


@step_tool
def classify_invoice(invoice_id: int, invoice_type: str = "") -> dict:
    """Decide what a draft really is once Experio has read it: a purchase (achat), a sale (vente), a bank statement
    (rb) or another document (autre). Sets the invoice's type in Experio when it is wrong; writes nothing for rb
    or autre (a stop result).

    The side comes, in order, from: an import folder's piece (always achat), the dossier's ICE or IF read on the
    invoice (side_by_ice), the side already stored, else the model reading the scan.
    ``invoice_type``: achat or vente as the client said (checked by split_invoices). A scan on the other side
    stops the invoice; one the model reads as autre takes the client's side."""
    read = ["detected_supplier_ice", "detected_customer_ice", "detected_supplier_if", "detected_customer_if"]
    read += ["folder_id", "folder_type"]
    invoice = require_invoice(
        invoice_id, ["state", "type", "company_id", "exercice_id", "bluejay_state", "document_pdf", "file_type", *read]
    )
    reading = still_reading(invoice)
    if reading:
        return reading
    if not has_scan(invoice):
        return routine(f"Facture {invoice_id} : pas de scan PDF a classer.")
    company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=["name", "ice", "vat"])[0]
    proven = side_by_ice(invoice, company)
    kept = _kept_classification(invoice_id, invoice_type)
    if invoice["folder_id"] and invoice["folder_type"] == "import":  # an import folder's piece: a purchase, always
        category, reason = "achat", f"pièce du dossier d'import `{invoice['folder_id'][1]}`"
        _save_classification(invoice_id, category, reason)
    elif proven:
        whose = "émetteur" if proven == "vente" else "acheteur"
        category, reason = proven, f"l'{whose} lu par Experio porte l'ICE ou l'IF du dossier `{company['name']}`"
        _save_classification(invoice_id, category, reason)
    elif kept:
        category, reason = kept
    else:
        pages = scan_pages(invoice["document_pdf"])
        category, reason = classify_pages(pages, "Classement de la facture", company["name"])
        _save_classification(invoice_id, category, reason)
    if category == "autre" and invoice_type in CATEGORY_TO_TYPE:
        # The client said which side it is (a customs slip booked as a purchase): their word, over the model's doubt.
        category, reason = invoice_type, f"donnée comme {invoice_type} par vous ; {reason}"
        _save_classification(invoice_id, category, reason)
    if category in ("rb", "autre"):
        what = "un relevé bancaire" if category == "rb" else "un autre document"
        return stop(
            f"Facture {invoice_id} : ce n'est pas une facture mais {what} ({reason}). Rien n'a été modifié.",
            category=category,
        )
    if invoice_type in CATEGORY_TO_TYPE and category != invoice_type:
        return stop(
            f"Facture {invoice_id} : vous l'avez donnée comme {invoice_type}, mais elle ressemble à une {category} "
            f"({reason}). Laissée en brouillon, rien n'a été modifié : vérifiez-la dans Experio.",
            category=category,
        )
    wanted = CATEGORY_TO_TYPE[category]
    if invoice["type"] == wanted:
        already = f"Facture {invoice_id} : {category}, déjà enregistrée comme telle ({reason})."
        return routine(already, category=category)
    require_draft(invoice_id, invoice, "changer de type")
    with refused(f"classify_invoice {invoice_id}", f"Facture {invoice_id} : Experio a refusé le changement de type."):
        # Experio's onchange on type runs, as on screen.
        fill_invoice(invoice_id, {"type": wanted}, invoice_context(invoice))
    changed = "le type passerait a" if test_mode_note() else "type change en"
    return ok(f"{test_mode_note()}Facture {invoice_id} : {category} ({reason}), {changed} {wanted}.", category=category)
