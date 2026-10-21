"""Purchase, sale, or not an invoice at all."""

import re

from tarik.core.app import ToolError, log, ok, routine, step_tool, stop
from tarik.core.cabinets import cabinet_db
from tarik.core.experio import (
    fill_invoice,
    has_scan,
    invoice_context,
    refused,
    require_draft,
    require_invoice,
    still_reading,
    test_mode_note,
)
from tarik.core.model import READER, ask_model, scan_pages, tool_prompt
from tarik.core.queries import Q
from tarik.core.words import normalized

CATEGORY_TO_TYPE = {"achat": "purchase", "vente": "sale"}
# How a client says which side their invoices are on (matched on normalized words).
SIDE_WORDS = {"achat": r"achats?|fournisseurs?|purchases?", "vente": r"ventes?|sales?|factures? (?:de )?clients?"}
SIDES = {"achat": {"achat"}, "vente": {"vente"}, "mixte": {"achat", "vente"}}


def classify_pages(pages, what):
    """The model's (category, reason) for a document's ``pages``: achat, vente, rb (bank statement) or autre
    (neither: a contract, a schedule, a screenshot). ``what`` names the step in logs and errors."""
    result = ask_model(
        tool_prompt("classify-document"),
        pages,
        'Answer with only a JSON object: {"category": "achat"|"vente"|"rb"|"autre", "reason": "one French sentence"}',
        what,
    )
    if result.get("category") not in ("achat", "vente", "rb", "autre"):
        log(f"{what}: unexpected category {result.get('category')!r}")
        raise ToolError(
            f"{what} : {READER} n'a pas su dire si c'est une facture d'achat, de vente ou un relevé bancaire ; "
            "precisez-le dans votre message."
        )
    return result["category"], str(result.get("reason") or "")


def side_of(invoice):
    """The side of ``invoice`` (with id and type), "purchase" or "sale": from its classification when there is one,
    else Experio's type. ToolError for a bank statement or neither side."""
    with cabinet_db() as db:
        row = db.execute(Q.classification_of_invoice, (invoice["id"],)).fetchone()
    if row and row[0] in ("rb", "autre"):
        raise ToolError(f"Facture {invoice['id']} : ce n'est pas une facture.")
    side = CATEGORY_TO_TYPE[row[0]] if row else invoice["type"]
    if side not in ("purchase", "sale"):
        raise ToolError(f"Facture {invoice['id']} : ni achat ni vente, classez-la d'abord avec classify_invoice.")
    return side


def client_side(invoice_type, words):
    """``invoice_type`` (achat, vente, or mixte for both) when the client wrote it themselves, else None:
    a model may not choose the side of the client's invoices."""
    # Each side's words, as whole words.
    written = {
        side for side, pattern in SIDE_WORDS.items() if re.search(rf"\b(?:{pattern})\b", normalized(words or ""))
    }
    return invoice_type if invoice_type in SIDES and SIDES[invoice_type] <= written else None


def _kept_classification(invoice_id, invoice_type):
    """The (category, reason) already decided for the invoice, or None: a draft taken up again keeps its side, the
    model's second look must not flip it. The client giving a side that differs asks for a new look."""
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
    """Decide what a draft really is once Experio has read it: a purchase
    (achat), a sale (vente) or a bank statement (rb, not an invoice at all).
    Sets the invoice's type when Experio has it wrong; writes nothing for a
    bank statement. ``invoice_type``: achat or vente as the client said (checked
    by split_invoices); a scan that says the other side stops the invoice, one the model reads as neither
    (autre) takes the client's side."""
    invoice = require_invoice(
        invoice_id, ["state", "type", "company_id", "exercice_id", "bluejay_state", "document_pdf", "file_type"]
    )
    reading = still_reading(invoice)
    if reading:
        return reading
    if not has_scan(invoice):
        return routine(f"Facture {invoice_id} : pas de scan PDF a classer.")
    kept = _kept_classification(invoice_id, invoice_type)
    if kept:
        category, reason = kept
    else:
        category, reason = classify_pages(scan_pages(invoice["document_pdf"]), "Classement de la facture")
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
            f"({reason}). Laissee en brouillon, rien n'a ete modifie : verifiez-la dans Experio.",
            category=category,
        )
    wanted = CATEGORY_TO_TYPE[category]
    if invoice["type"] == wanted:
        already = f"Facture {invoice_id} : {category}, déjà enregistrée comme telle ({reason})."
        return routine(already, category=category)
    require_draft(invoice_id, invoice, "changer de type")
    with refused(f"classify_invoice {invoice_id}", f"Facture {invoice_id} : Experio a refusé le changement de type."):
        fill_invoice(
            invoice_id, {"type": wanted}, invoice_context(invoice)
        )  # Experio's onchange on type runs, as on screen
    changed = "le type passerait a" if test_mode_note() else "type change en"
    return ok(f"{test_mode_note()}Facture {invoice_id} : {category} ({reason}), {changed} {wanted}.", category=category)
