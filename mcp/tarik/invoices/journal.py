"""Phase 10: the invoice's journal.

tarik-master moves foreign invoices to the foreign journal and runs a 4-tool agent to find
or create one of the right type. Experio already picks it when it refreshes an invoice
(get_invoice_journal_id): the dossier's local or foreign purchase or sale journal, FNP/FAE
journals, the folder's for customs, or the one chosen by hand in multi-journal dossiers.
So this step only makes sure the invoice sits there, and says which setting is missing
when the dossier has none -- creating journals is the cabinet's setup, not Tarik's.
"""

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server, with_ids
from tarik.core.experio import DRY_RUN, invoice_context, odoo_call, odoo_json, require_invoice

SETTING = {  # (type, local?) -> the dossier setting Experio takes the journal from
    ("purchase", True): ("local_purchase_journal_id", "journal d'achats local"),
    ("purchase", False): ("foreign_purchase_journal_id", "journal d'achats etranger"),
    ("sale", True): ("local_sale_journal_id", "journal de ventes local"),
    ("sale", False): ("foreign_sale_journal_id", "journal de ventes etranger"),
}


@server.tool()
@with_ids
def check_journal(invoice_id: int) -> dict:
    """Make sure a draft invoice sits in the journal Experio's rule gives it, and say
    which dossier setting is missing when there is none."""
    invoice = require_invoice(invoice_id, ["state", "type", "is_local_invoice", "company_id", "exercice_id", "journal_id",
                                           "enable_multi_journal", "folder_id", "is_fnp_invoice"])
    if invoice["enable_multi_journal"] and invoice["journal_id"] or invoice["folder_id"] or invoice["is_fnp_invoice"]:
        return ok(f"Facture {invoice_id} : journal {(invoice['journal_id'] or [0, 'non renseigne'])[1]} (choix propre au dossier).")
    field, label = SETTING[(invoice["type"], bool(invoice["is_local_invoice"]))]
    expected = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=[field])[0][field]
    if not expected:
        return ok(f"Facture {invoice_id} : le dossier n'a pas de {label} configure, a regler par le cabinet "
                  "(Configuration > Parametres) avant de comptabiliser.")
    if invoice["journal_id"] and invoice["journal_id"][0] == expected[0]:
        return ok(f"Facture {invoice_id} : journal {expected[1]}.")
    if DRY_RUN:
        return ok(f"MODE TEST : aucune modification enregistree dans Experio. Facture {invoice_id} : "
                  f"elle passerait au journal {expected[1]}.")
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut changer de journal.")
    context = invoice_context(invoice)
    try:
        odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)  # Experio sets its own journal
        now = odoo_call("exp.invoice", "read", [invoice_id], fields=["journal_id"], context=context)[0]["journal_id"]
    except Exception as error:
        print(f"check_journal {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio n'a pas pu mettre a jour le journal.") from None
    if not now or now[0] != expected[0]:
        raise ToolError(f"Facture {invoice_id} : Experio a garde le journal {(now or [0, 'vide'])[1]} au lieu de {expected[1]}.")
    return ok(f"Facture {invoice_id} : journal {expected[1]}.")
