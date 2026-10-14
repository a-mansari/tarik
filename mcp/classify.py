"""Phase 5: purchase, sale, or not an invoice at all (tarik-master agents/classify.py)."""

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from app import server
from documents import scan_pages
from experio import DRY_RUN, POSTGRES, fill_invoice, invoice_context, require_invoice
from model import ask_model


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

@server.tool()
def classify_invoice(invoice_id: int) -> str:
    """Decide what a draft really is once Experio has read it: a purchase
    (achat), a sale (vente) or a bank statement (rb, not an invoice at all).
    Sets the invoice's type when Experio has it wrong; writes nothing for a
    bank statement."""
    invoice = require_invoice(invoice_id, ["state", "type", "company_id", "exercice_id", "bluejay_state", "document_pdf", "file_type"])
    if invoice["bluejay_state"] in ("pending", "sent"):
        return f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute."
    if not invoice["document_pdf"] or invoice["file_type"] not in (False, None, "pdf"):
        return f"Facture {invoice_id} : pas de scan PDF a classer."
    result = ask_model(CLASSIFY_PROMPT, scan_pages(invoice["document_pdf"]),
                       'Answer with only a JSON object: {"category": "achat"|"vente"|"rb", "reason": "one French sentence"}',
                       "Classement de la facture")
    category, reason = result.get("category"), str(result.get("reason") or "")
    if category not in ("achat", "vente", "rb"):
        print(f"classify_invoice {invoice_id}: unexpected category {category!r}", flush=True)
        raise ToolError("Classement de la facture : echec, reessayez plus tard.")
    with psycopg.connect(**POSTGRES) as db:
        db.execute("insert into tarik_classifications (invoice_id, category, reason) values (%s, %s, %s) "
                   "on conflict (invoice_id) do update set category = excluded.category, reason = excluded.reason, classified_at = now()",
                   (invoice_id, category, reason))
    if category == "rb":
        return f"Facture {invoice_id} : ce n'est pas une facture mais un releve bancaire ({reason}). Rien n'a ete modifie."
    wanted = CATEGORY_TO_TYPE[category]
    if invoice["type"] == wanted:
        return f"Facture {invoice_id} : {category}, deja enregistree comme telle ({reason})."
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : {category}, mais seule une facture brouillon peut changer de type.")
    context = invoice_context(invoice)
    try:
        fill_invoice(invoice_id, {"type": wanted}, context)  # Experio's onchange on type runs, as on screen
    except Exception as error:
        print(f"classify_invoice {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio a refuse le changement de type.") from None
    if DRY_RUN:
        return f"MODE TEST : aucune modification enregistree dans Experio. Facture {invoice_id} : {category} ({reason}), le type passerait a {wanted}."
    return f"Facture {invoice_id} : {category} ({reason}), type change en {wanted}."
