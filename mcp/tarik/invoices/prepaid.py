"""Phase 12: charges (or produits) constatees d'avance -- a billed period over several months.

Bluejay doesn't detect the period; check_invoice reads it on the scan. When it spans several
months, tarik-master asks the client whether to spread the charge. Experio does the rest:
ticking it in the form fills and validates the period, and posting generates the spreading
entries with the dossier's prepaid account and journal.
"""

from tarik.core.app import ok, question, server, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.conversation import yes_no
from tarik.core.experio import (
    experio_notes,
    fill_invoice,
    invoice_context,
    refused,
    require_draft,
    require_invoice,
    test_mode_note,
)


def prepaid_period(start, end):
    """The billed period from ``start`` to ``end`` (YYYY-MM-DD) as Experio's prepaid fields, when it spans several
    months (tarik-master odoo.prepaid_period), else None."""
    if not (start and end and len(start) == len(end) == 10 and start < end):
        return None
    months = (int(end[:4]) - int(start[:4])) * 12 + int(end[5:7]) - int(start[5:7]) + 1
    if months < 2:
        return None
    return {"prepaid_start_date": start, "prepaid_end_date": end, "prepaid_months": months}


def _billed_period(invoice_id):
    """The period check_invoice read on the scan, as prepaid_period gives it, else None."""
    with cabinet_db() as db:
        row = db.execute(
            "select extracted->>'period_start', extracted->>'period_end' from tarik_checks where invoice_id = %s",
            (invoice_id,),
        ).fetchone()
    return prepaid_period(*row) if row else None


@server.tool()
@with_ids
def settle_prepaid(invoice_id: int, answer: str = "") -> dict:
    """When a draft invoice bills a period over several months, ask whether to spread it
    (charges or produits constates d'avance); call again with the client's oui/non."""
    invoice = require_invoice(
        invoice_id, ["state", "type", "company_id", "exercice_id", "invoice_number", "has_prepaid_expenses"]
    )
    if invoice["has_prepaid_expenses"]:
        return ok(f"Facture {invoice_id} : deja repartie en constatee d'avance.")
    values = _billed_period(invoice_id)
    if not values:
        return ok(f"Facture {invoice_id} : pas de periode sur plusieurs mois, rien a repartir.")
    kind = "produits" if invoice["type"] == "sale" else "charges"
    asked = (
        f"Facture {invoice['invoice_number'] or invoice_id} : elle couvre {values['prepaid_months']} mois "
        f"({values['prepaid_start_date']} au {values['prepaid_end_date']}). "
        f"La repartir en {kind} constates d'avance ? oui / non"
    )
    spread = yes_no(answer) if answer else None
    if spread is None:
        return question(asked if not answer else f"Reponse non comprise. {asked}")
    if not spread:
        return ok(f"Facture {invoice_id} : pas de repartition.")
    require_draft(invoice_id, invoice)
    context = invoice_context(invoice)
    with refused(f"settle_prepaid {invoice_id}", f"Facture {invoice_id} : Experio a refuse la repartition."):
        # Ticked first, as on screen (its onchange fills defaults), then the period read on the scan.
        fill_invoice(invoice_id, {"has_prepaid_expenses": True, **values}, context)
        experio = experio_notes(invoice_id, context)
    return ok(
        f"{test_mode_note()}Facture {invoice_id} : {kind} constates d'avance sur {values['prepaid_months']} mois "
        f"a partir du {values['prepaid_start_date']} (ecritures generees a la comptabilisation).{experio}"
    )
