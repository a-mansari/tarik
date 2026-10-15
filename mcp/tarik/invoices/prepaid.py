"""Phase 12: charges (or produits) constatees d'avance -- a billed period over several months.

Bluejay doesn't detect the period; check_invoice reads it on the scan. When it spans several
months, tarik-master asks the client whether to spread the charge. Experio does the rest:
ticking it in the form fills and validates the period, and posting generates the spreading
entries with the dossier's prepaid account and journal.
"""

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, question, server, with_ids
from tarik.dossiers.search import normalized
from tarik.core.experio import DRY_RUN, POSTGRES, fill_invoice, invoice_context, require_invoice
from tarik.invoices.tva import experio_messages

# tarik-master read the yes/no with a model call; these words, in code, are enough and never drift.
YES = ("oui", "yes", "ok", "d'accord", "vas-y", "vas y", "repartis", "repartir", "c'est ca", "exact")
NO = ("non", "no", "pas besoin", "ne pas", "ponctuel", "inutile")


def yes_no(reply):
    """True, False, or None when the reply says neither."""
    text = normalized(reply or "")
    if any(text.startswith(normalized(word)) for word in NO):
        return False
    if any(text.startswith(normalized(word)) for word in YES):
        return True
    return None


def prepaid_period(start, end):
    """The billed period when it spans several months (tarik-master odoo.prepaid_period), else None."""
    if not (start and end and len(start) == len(end) == 10 and start < end):
        return None
    months = (int(end[:4]) - int(start[:4])) * 12 + int(end[5:7]) - int(start[5:7]) + 1
    if months < 2:
        return None
    return {"prepaid_start_date": start, "prepaid_end_date": end, "prepaid_months": months}


@server.tool()
@with_ids
def settle_prepaid(invoice_id: int, answer: str = "") -> dict:
    """When a draft invoice bills a period over several months, ask whether to spread it
    (charges or produits constates d'avance); call again with the client's oui/non."""
    invoice = require_invoice(invoice_id, ["state", "type", "company_id", "exercice_id", "invoice_number", "has_prepaid_expenses"])
    if invoice["has_prepaid_expenses"]:
        return ok(f"Facture {invoice_id} : deja repartie en constatee d'avance.")
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select extracted->>'period_start', extracted->>'period_end' from tarik_checks where invoice_id = %s",
                         (invoice_id,)).fetchone()
    values = prepaid_period(*row) if row else None
    if not values:
        return ok(f"Facture {invoice_id} : pas de periode sur plusieurs mois, rien a repartir.")
    kind = "produits" if invoice["type"] == "sale" else "charges"
    asked = (f"Facture {invoice['invoice_number'] or invoice_id} : elle couvre {values['prepaid_months']} mois "
             f"({values['prepaid_start_date']} au {values['prepaid_end_date']}). La repartir en {kind} constates d'avance ? oui / non")
    spread = yes_no(answer) if answer else None
    if spread is None:
        return question(asked if not answer else f"Reponse non comprise. {asked}")
    if not spread:
        return ok(f"Facture {invoice_id} : pas de repartition.")
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut etre modifiee.")
    context = invoice_context(invoice)
    try:
        # Ticked first, as on screen (its onchange fills defaults), then the period read on the scan.
        fill_invoice(invoice_id, {"has_prepaid_expenses": True, **values}, context)
        messages = experio_messages(invoice_id, context)
    except Exception as error:
        print(f"settle_prepaid {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio a refuse la repartition.") from None
    head = "MODE TEST : aucune modification enregistree dans Experio. " if DRY_RUN else ""
    return ok(f"{head}Facture {invoice_id} : {kind} constates d'avance sur {values['prepaid_months']} mois a partir du "
              f"{values['prepaid_start_date']} (ecritures generees a la comptabilisation)." + ("" if not messages else " " + " ".join(messages)))
