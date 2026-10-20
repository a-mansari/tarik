"""The invoice's journal.

Experio picks it when it refreshes an invoice
(get_invoice_journal_id): the dossier's local or foreign purchase or sale journal, FNP/FAE
journals, the folder's for customs, or the one chosen by hand in multi-journal dossiers.
So this step only makes sure the invoice sits there, and says which setting is missing
when the dossier has none -- creating journals is the cabinet's setup, not Tarik's.
"""

from tarik.core.app import ToolError, ok, routine, step_tool, stop
from tarik.core.config import TEST_MODE
from tarik.core.experio import (
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
    ("purchase", False): ("foreign_purchase_journal_id", "journal d'achats etranger", ACHAT + "Achat a l'etranger"),
    ("sale", True): ("local_sale_journal_id", "journal de ventes local", VENTE + "Vente locale"),
    ("sale", False): ("foreign_sale_journal_id", "journal de ventes etranger", VENTE + "Vente a l'etranger"),
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


def chosen_by_dossier(invoice):
    """True when the journal is the dossier's own choice, not Experio's rule from the settings."""
    multi = invoice["enable_multi_journal"] and invoice["journal_id"]
    return bool(multi or invoice["folder_id"] or invoice["is_fnp_invoice"])


def journal_stop(invoice_id, invoice):
    """A stop result when the dossier's journal setting for the invoice is unset or another dossier's (Experio's
    default for a new dossier, which Experio then refuses to read inside it), else None. ``invoice``: its
    JOURNAL_FIELDS."""
    if chosen_by_dossier(invoice):
        return None
    field, label, where = SETTING[(invoice["type"], bool(invoice["is_local_invoice"]))]
    wrong = misplaced_journals(invoice["company_id"][0], [field])
    if not wrong:
        return None
    start = f"Facture {invoice_id} : le dossier n'a pas de {label} configure"
    if wrong[field]:
        start = f"Facture {invoice_id} : le {label} du dossier ({wrong[field]}) est celui d'un autre dossier"
    return stop(
        f"{start}, a regler par le cabinet avant de continuer : creer ou importer les journaux du dossier "
        f"(Configuration > Comptabilite > Journaux), puis le choisir dans Configuration > {where}. "
        "Rien n'a ete modifie."
    )


@step_tool
def check_journal(invoice_id: int) -> dict:
    """Make sure a draft invoice sits in the journal Experio's rule gives it, and say
    which dossier setting is missing, or is another dossier's, when it is."""
    invoice = require_invoice(invoice_id, JOURNAL_FIELDS)
    if chosen_by_dossier(invoice):
        journal = (invoice["journal_id"] or [0, "non renseigne"])[1]
        return routine(f"Facture {invoice_id} : journal {journal} (choix propre au dossier).")
    if refusal := journal_stop(invoice_id, invoice):
        return refusal
    field = SETTING[(invoice["type"], bool(invoice["is_local_invoice"]))][0]
    expected = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=[field])[0][field]
    if invoice["journal_id"] and invoice["journal_id"][0] == expected[0]:
        return routine(f"Facture {invoice_id} : journal {expected[1]}.")
    require_draft(invoice_id, invoice, "changer de journal")
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id} : elle passerait au journal {expected[1]}.")
    context = invoice_context(invoice)
    with refused(f"check_journal {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu mettre a jour le journal."):
        refresh(invoice_id, context)  # Experio sets its own journal
        now = odoo_call("exp.invoice", "read", [invoice_id], fields=["journal_id"], context=context)[0]["journal_id"]
    if not now or now[0] != expected[0]:
        raise ToolError(
            f"Facture {invoice_id} : Experio a garde le journal {(now or [0, 'vide'])[1]} au lieu de {expected[1]}."
        )
    return routine(f"Facture {invoice_id} : journal {expected[1]}.")
