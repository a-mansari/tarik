"""Phase 5: purchase, sale, or not an invoice at all (tarik-master agents/classify.py)."""

import re

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import with_ids, ok, server, stop, wait
from tarik.core.documents import scan_pages
from tarik.core.cabinets import cabinet_db
from tarik.core.experio import fill_invoice, invoice_context, refused, require_draft, require_invoice, test_mode_note
from tarik.core.model import ask_model
from tarik.dossiers.search import normalized


# tarik-master/tarik/agents/classify.py, verbatim.
CLASSIFY_PROMPT = """You look at a scanned document from a Moroccan accounting cabinet
and decide what it is, before anything else is done with it.

- achat: a supplier invoice ("Facture", "Facture N°") addressed TO the cabinet's
  client -- the cabinet's client is the buyer, named in "Facturé à" / "Bill to".
- vente: an invoice the cabinet's client itself issued to one of ITS OWN
  customers -- the cabinet's client is the seller, its own letterhead is at the
  top billing someone else.
- rb: a releve bancaire (bank statement) -- a list of transactions/movements
  over a period, issued by a bank, not a single invoice with a HT/TVA/TTC total.

Look at the whole page: who bills whom, and whether it is a single invoice or a
table of bank movements. Give a one-sentence reason in French.
"""

CATEGORY_TO_TYPE = {"achat": "purchase", "vente": "sale"}
# How a client says which side their invoices are on (matched on normalized words).
SIDE_WORDS = {"achat": r"achats?|fournisseurs?|purchases?", "vente": r"ventes?|sales?|factures? (?:de )?clients?"}
SIDES = {"achat": {"achat"}, "vente": {"vente"}, "mixte": {"achat", "vente"}}


def classify_pages(pages, what):
    """The model's (category, reason) for a document's pages: achat, vente or rb (bank statement)."""
    result = ask_model(CLASSIFY_PROMPT, pages,
                       'Answer with only a JSON object: {"category": "achat"|"vente"|"rb", "reason": "one French sentence"}', what)
    if result.get("category") not in ("achat", "vente", "rb"):
        print(f"{what}: unexpected category {result.get('category')!r}", flush=True)
        raise ToolError(f"{what} : echec, reessayez plus tard.")
    return result["category"], str(result.get("reason") or "")


def side_of(invoice):
    """The invoice's side, purchase or sale: from its classification when there is one, else Experio's type."""
    with cabinet_db() as db:
        row = db.execute("select category from tarik_classifications where invoice_id = %s", (invoice["id"],)).fetchone()
    if row and row[0] == "rb":
        raise ToolError(f"Facture {invoice['id']} : c'est un releve bancaire, pas une facture.")
    side = CATEGORY_TO_TYPE[row[0]] if row else invoice["type"]
    if side not in ("purchase", "sale"):
        raise ToolError(f"Facture {invoice['id']} : ni achat ni vente, classez-la d'abord avec classify_invoice.")
    return side


def client_side(invoice_type, words):
    """``invoice_type`` (achat, vente, or mixte for both) when the client wrote it themselves, else None:
    a model may not choose the side of the client's invoices."""
    written = {side for side, pattern in SIDE_WORDS.items() if re.search(rf"\b(?:{pattern})\b", normalized(words or ""))}
    return invoice_type if invoice_type in SIDES and SIDES[invoice_type] <= written else None


@server.tool()
@with_ids
def classify_invoice(invoice_id: int, invoice_type: str = "") -> dict:
    """Decide what a draft really is once Experio has read it: a purchase
    (achat), a sale (vente) or a bank statement (rb, not an invoice at all).
    Sets the invoice's type when Experio has it wrong; writes nothing for a
    bank statement. ``invoice_type``: achat or vente as the client said (checked
    by split_invoices); a scan that says otherwise stops the invoice."""
    invoice = require_invoice(invoice_id, ["state", "type", "company_id", "exercice_id", "bluejay_state", "document_pdf", "file_type"])
    if invoice["bluejay_state"] in ("pending", "sent"):
        return wait(f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute.")
    if not invoice["document_pdf"] or invoice["file_type"] not in (False, None, "pdf"):
        return ok(f"Facture {invoice_id} : pas de scan PDF a classer.")
    category, reason = classify_pages(scan_pages(invoice["document_pdf"]), "Classement de la facture")
    with cabinet_db() as db:
        db.execute("insert into tarik_classifications (invoice_id, category, reason) values (%s, %s, %s) "
                   "on conflict (invoice_id) do update set category = excluded.category, reason = excluded.reason, classified_at = now()",
                   (invoice_id, category, reason))
    if category == "rb":
        return stop(f"Facture {invoice_id} : ce n'est pas une facture mais un releve bancaire ({reason}). Rien n'a ete modifie.", category=category)
    if invoice_type in CATEGORY_TO_TYPE and category != invoice_type:
        return stop(f"Facture {invoice_id} : vous l'avez donnee comme {invoice_type}, mais elle ressemble a une {category} "
                    f"({reason}). Laissee en brouillon, rien n'a ete modifie : verifiez-la dans Experio.", category=category)
    wanted = CATEGORY_TO_TYPE[category]
    if invoice["type"] == wanted:
        return ok(f"Facture {invoice_id} : {category}, deja enregistree comme telle ({reason}).", category=category)
    require_draft(invoice_id, invoice, "changer de type")
    with refused(f"classify_invoice {invoice_id}", f"Facture {invoice_id} : Experio a refuse le changement de type."):
        fill_invoice(invoice_id, {"type": wanted}, invoice_context(invoice))  # Experio's onchange on type runs, as on screen
    changed = "le type passerait a" if test_mode_note() else "type change en"
    return ok(f"{test_mode_note()}Facture {invoice_id} : {category} ({reason}), {changed} {wanted}.", category=category)
