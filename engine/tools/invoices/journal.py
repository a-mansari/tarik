"""Make sure the invoice sits in the journal Experio's rule gives it.

Experio picks the journal when it refreshes an invoice (exp.invoice get_invoice_journal_id): the one chosen by hand
in a multi-journal dossier, else the import folder's, else the FNP/FAE journal when set, else the dossier's local or
foreign purchase or sale journal. So this step only checks the invoice sits there, and reports which setting is
missing when the dossier has none: creating journals is the cabinet's setup, not Tarik's.
"""

from engine.tools.core.app import ToolError, ok, routine, step_tool, stop
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    dry_run,
    invoice_context,
    misplaced_journals,
    odoo_call,
    refresh,
    refused,
    require_draft,
    require_invoice,
)

ACHAT, VENTE = "General > Achat, carte ", "General > Vente, carte "
SETTING = {  # (type, local?) -> the dossier setting Experio takes the journal from, and where it is set
    ("purchase", True): ("local_purchase_journal_id", "journal d'achats local", ACHAT + "Achat local"),
    ("purchase", False): ("foreign_purchase_journal_id", "journal d'achats etranger", ACHAT + "Achat à l'etranger"),
    ("sale", True): ("local_sale_journal_id", "journal de ventes local", VENTE + "Vente locale"),
    ("sale", False): ("foreign_sale_journal_id", "journal de ventes etranger", VENTE + "Vente à l'etranger"),
}


JOURNAL_FIELDS = [
    "state",
    "type",
    "is_local_invoice",
    "company_id",
    "exercice_id",
    "journal_id",
    "enable_multi_journal",
    "folder_id",
    "is_fnp_invoice",
]


FNP_JOURNAL = {"purchase": "fnp_supplier_journal_id", "sale": "fae_customer_journal_id"}  # the dossier's FNP / FAE


def chosen_by_dossier(invoice):
    """True when the journal is not taken from the dossier's purchase or sale journal settings: chosen by hand in a
    multi-journal dossier, an import folder's, or an FNP/FAE invoice's when the dossier has an FNP/FAE journal. Without
    one, Experio falls back to the settings journal for an FNP/FAE invoice too (exp_invoice.py get_invoice_journal_id),
    so that one is checked."""
    multi = invoice["enable_multi_journal"] and invoice["journal_id"]
    if multi or invoice["folder_id"]:
        return True
    if not invoice["is_fnp_invoice"]:
        return False
    field = FNP_JOURNAL[invoice["type"]]
    return bool(odoo_call("res.company", "read", [invoice["company_id"][0]], fields=[field])[0][field])


def journal_stop(invoice_id, invoice):
    """Return a stop result when the dossier's journal setting for the invoice is unset or holds another dossier's
    journal (Experio's default for a new dossier, which Experio then refuses to read inside it); else None.
    ``invoice``: its JOURNAL_FIELDS."""
    if chosen_by_dossier(invoice):
        return None
    field, label, where = SETTING[(invoice["type"], bool(invoice["is_local_invoice"]))]
    wrong = misplaced_journals(invoice["company_id"][0], [field])
    if not wrong:
        return None
    start = f"Facture {invoice_id} : le dossier n'a pas de {label} configuré"
    if wrong[field]:
        start = f"Facture {invoice_id} : le {label} du dossier (`{wrong[field]}`) est celui d'un autre dossier"
    return stop(
        f"{start}, à régler par le cabinet avant de continuer : créer ou importer les journaux du dossier "
        f"(Configuration > Comptabilite > Journaux), puis le choisir dans Configuration > {where}. "
        "Rien n'a été modifié."
    )


@step_tool
def check_journal(invoice_id: int) -> dict:
    """Make sure a draft invoice sits in the journal Experio's rule gives it, moving it there with Experio's
    refresh. A stop result names the dossier setting that is missing or holds another dossier's journal.
    Raises ToolError when Experio keeps another journal."""
    invoice = require_invoice(invoice_id, JOURNAL_FIELDS)
    if chosen_by_dossier(invoice):
        journal = f"`{invoice['journal_id'][1]}`" if invoice["journal_id"] else "non renseigné"
        return routine(f"Facture {invoice_id} : journal {journal} (choix propre au dossier).")
    refusal = journal_stop(invoice_id, invoice)
    if refusal:
        return refusal
    field = SETTING[(invoice["type"], bool(invoice["is_local_invoice"]))][0]
    expected = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=[field])[0][field]
    if invoice["journal_id"] and invoice["journal_id"][0] == expected[0]:
        return routine(f"Facture {invoice_id} : journal `{expected[1]}`.")
    require_draft(invoice_id, invoice, "changer de journal")
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id} : elle passerait au journal `{expected[1]}`.")
    context = invoice_context(invoice)
    with refused(f"check_journal {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu mettre a jour le journal."):
        refresh(invoice_id, context)  # Experio sets its own journal
        now = odoo_call("exp.invoice", "read", [invoice_id], fields=["journal_id"], context=context)[0]["journal_id"]
    if not now or now[0] != expected[0]:
        kept = f"`{now[1]}`" if now else "vide"
        raise ToolError(f"Facture {invoice_id} : Experio a gardé le journal {kept} au lieu de `{expected[1]}`.")
    return routine(f"Facture {invoice_id} : journal `{expected[1]}`.")
