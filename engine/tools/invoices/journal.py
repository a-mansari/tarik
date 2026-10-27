"""Make sure the invoice sits in the journal Experio's rule gives it.

Experio picks the journal when it refreshes an invoice (exp.invoice get_invoice_journal_id): the one chosen by hand
in a multi-journal dossier, else the import folder's, else the FNP/FAE journal when set, else the dossier's local or
foreign purchase or sale journal. So this step checks the invoice sits there. When that setting is unset or holds
another dossier's journal, Tarik sets it as Experio's own « Auto-Detect » does (res.company auto_detect_settings:
the dossier's first journal of the type), first making the journal when the dossier has none of the type (Experio's
journal create then runs that same auto-detection), and tells the client.
"""

from engine.tools.core.app import ToolError, ok, routine, step_tool, stop
from engine.tools.core.cabinets import current
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    dossier_lock,
    dry_run,
    in_dossier,
    invoice_context,
    misplaced_journals,
    odoo_call,
    odoo_json,
    refresh,
    refused,
    require_draft,
    require_invoice,
    would,
)
from engine.tools.dossiers.banks import free_code

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


NEW_JOURNAL = {"purchase": ("Achats", "ACH"), "sale": ("Ventes", "VTE")}  # the journal made when none: name, code
# What Tarik set, by (cabinet, dossier), for the next check_journal to tell the client once.
# shortcut: kept in this process only, a restart in between loses the note (the setting itself is kept by Experio).
SET = {}


def set_journal(company_id, kind):
    """Set the dossier's empty or misplaced journal settings as Experio's « Auto-Detect » does, first making a
    ``kind`` ("purchase" or "sale") journal when the dossier has none (NEW_JOURNAL, its code free in the dossier).
    Returns what was done (French), kept in SET for check_journal; in test mode nothing is written and the text says
    what would be done."""
    context = in_dossier(company_id)
    same = odoo_call(
        "exp.journal",
        "search_read",
        [("company_id", "=", company_id), ("type", "=", kind)],
        fields=["name"],
        order="id",
        limit=1,
        context=context,
    )
    if same:
        done = f"journal `{same[0]['name']}` {would('repris')} dans la configuration du dossier"
        done += " (auto-détection d'Experio)"
        if not dry_run():
            odoo_json("res.company", "auto_detect_settings", [company_id], comodel_name="exp.journal", context=context)
    else:
        name, prefix = NEW_JOURNAL[kind]
        code = free_code(company_id, prefix, context)
        done = f"journal `{name}` (code `{code}`) {would('créé')} et mis dans la configuration du dossier"
        if not dry_run():  # Experio's create runs the auto-detection itself (exp_journal.py create)
            values = {"name": name, "code": code, "type": kind, "company_id": company_id}
            odoo_call("exp.journal", "create", values, context=context)
    if dry_run():
        return f"{TEST_MODE}{done}"
    SET[(current(), company_id)] = done
    return done


def journal_stop(invoice_id, invoice):
    """Return a stop result when the dossier's journal setting for the invoice is unset or holds another dossier's
    journal (Experio's default for a new dossier, which Experio then refuses to read inside it) and set_journal could
    not make it right; else None. ``invoice``: its JOURNAL_FIELDS."""
    if chosen_by_dossier(invoice):
        return None
    field, label, where = SETTING[(invoice["type"], bool(invoice["is_local_invoice"]))]
    company_id = invoice["company_id"][0]
    if not misplaced_journals(company_id, [field]):
        return None
    with (
        dossier_lock(company_id),  # the invoices of one message, in parallel, make the journal once
        refused(f"journal_stop {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu régler le {label}."),
    ):
        if misplaced_journals(company_id, [field]):  # not set meanwhile by another invoice
            done = set_journal(company_id, invoice["type"])
            if dry_run():
                return stop(f"Facture {invoice_id} : {done}.")
    wrong = misplaced_journals(company_id, [field])
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
def check_journal(invoice_id: int, tell: bool = True) -> dict:
    """Make sure a draft invoice sits in the journal Experio's rule gives it, moving it there with Experio's
    refresh. A stop result names the dossier setting that is missing or holds another dossier's journal.
    ``tell``: the result says the journal set by Tarik for this dossier (SET), once; False for an early check that
    leaves it to the journal step. Raises ToolError when Experio keeps another journal."""
    invoice = require_invoice(invoice_id, JOURNAL_FIELDS)
    if chosen_by_dossier(invoice):
        journal = f"`{invoice['journal_id'][1]}`" if invoice["journal_id"] else "non renseigné"
        return routine(f"Facture {invoice_id} : journal {journal} (choix propre au dossier).")
    refusal = journal_stop(invoice_id, invoice)
    if refusal:
        return refusal
    field = SETTING[(invoice["type"], bool(invoice["is_local_invoice"]))][0]
    expected = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=[field])[0][field]
    done = SET.pop((current(), invoice["company_id"][0]), "") if tell else ""
    if done:
        done = f" Fait : {done}."
    if invoice["journal_id"] and invoice["journal_id"][0] == expected[0]:
        result = ok if done else routine  # a journal set by Tarik is told, else nothing to tell
        return result(f"Facture {invoice_id} : journal `{expected[1]}`.{done}")
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
    return (ok if done else routine)(f"Facture {invoice_id} : journal `{expected[1]}`.{done}")
