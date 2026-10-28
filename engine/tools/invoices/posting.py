"""Post (« Comptabiliser ») a draft invoice, only when Experio's own check finds nothing blocking.

Everything goes through Experio's own methods: its refresh, collect_validation_messages (what the Comptabiliser
button checks), action_post, then the state read back, because action_post's return value says nothing reliable.
Posting also saves the invoice's account, non-recoverable TVA and RAS settings on its tiers for the next invoices
(exp.invoice.memorize_partner_values).
"""

import time

from engine.tools.core.app import log, ok, routine, step_tool, stop
from engine.tools.core.config import POSTED, TEST_MODE
from engine.tools.core.experio import (
    blocking,
    dry_run,
    experio_refusal,
    invoice_context,
    odoo_call,
    odoo_json,
    plain,
    refresh,
    refused,
    require_invoice,
)
from engine.tools.invoices.configuration import assets_text, assets_without_category
from engine.tools.invoices.exercice import with_fiscal_year

RETRIES, PAUSE = 3, 1.0  # the check can lag right after a refresh
ACCOUNT = {
    "purchase": ("third_party_supplier_account", "fournisseur"),
    "sale": ("third_party_customer_account", "client"),
}


def missing_compte_tiers(invoice):
    """Return the role ("fournisseur" or "client") whose Compte Tiers the invoice's tiers lacks, else None.

    None also when the invoice has no tiers or the dossier ignores Compte Tiers (ignore_third_party_account).
    Experio posts without a Compte Tiers unless the dossier turned on its optional block
    (block_invoice_validation_without_third_party_account, validate_third_party_account), and a posting without one
    cannot be exported.
    """
    field, role = ACCOUNT.get(invoice["type"], (None, None))
    if not field or not invoice["partner_id"]:
        return None
    company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=["ignore_third_party_account"])[0]
    if company["ignore_third_party_account"]:
        return None
    partner = odoo_call("exp.partner", "read", [invoice["partner_id"][0]], fields=[field])[0]
    return None if partner[field] else role


def disbursements_first(invoice):
    """Link a transit agent's invoice to the folder's invoices it paid, before it is posted.

    Only for a transit invoice (invoice_type "transit") of an import folder. Returns a stop result while some
    invoices of the folder paid by the transit agent (« Payée par transitaire ») are still draft or pending, because
    Experio links only posted ones. Otherwise links the posted ones it lacks as Experio's form does
    (_onchange_autofill_disbursement_invoices: same folder, posted, not a transit invoice, not linked to another
    transit invoice), refreshes the invoice so its writing gets one disbursement line per linked invoice, and returns
    None. Returns None for any other invoice.
    """
    fields = ["invoice_type", "folder_id", "folder_type", "disbursement_invoice_ids", "company_id", "exercice_id"]
    found = odoo_call("exp.invoice", "read", [invoice["id"]], fields=fields)[0]
    if found["invoice_type"] != "transit" or not found["folder_id"] or found["folder_type"] != "import":
        return None
    folder = [("folder_id", "=", found["folder_id"][0]), ("is_disbursement_invoice", "=", True)]
    folder.append(("invoice_type", "!=", "transit"))
    waiting = odoo_call("exp.invoice", "search", [*folder, ("state", "in", ["pending", "draft"])])
    if waiting:
        return stop(
            f"Facture {invoice['id']} (transitaire) : pas comptabilisée avant les factures payées par le transitaire "
            f"du dossier ({', '.join(map(str, waiting))}), qu'Experio ne lui rattache qu'une fois comptabilisées."
        )
    eligible = odoo_call(
        "exp.invoice", "search", [*folder, ("state", "in", ["post", "export"]), ("transit_invoice_id", "=", False)]
    )
    missing = [i for i in eligible if i not in found["disbursement_invoice_ids"]]
    if missing and not dry_run():
        context = invoice_context(found)
        odoo_call(
            "exp.invoice",
            "write",
            [invoice["id"]],
            {"disbursement_invoice_ids": [(4, i) for i in missing]},
            context=context,
        )
        refresh(invoice["id"], context)  # its disbursement lines, one per linked invoice
    return None


def auto_merge(invoice):
    """Return a stop result when posting would merge two tiers for good, else None.

    When the dossier has auto_merge_detected_partner on and the tiers set on the invoice is not the one Experio read
    (has_wrong_partner), action_post merges the read tiers into the set one and deletes it
    (_merge_partners_and_flag_duplicates, then _auto_merge_detected_partner). Only the cabinet decides that.
    """
    company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=["auto_merge_detected_partner"])[0]
    if not company["auto_merge_detected_partner"]:
        return None
    wrong = odoo_call("exp.invoice", "read", [invoice["id"]], fields=["has_wrong_partner"])[0]["has_wrong_partner"]
    if not wrong:
        return None
    return stop(
        f"Facture {invoice['id']} : pas comptabilisée : le tiers choisi n'est pas celui qu'Experio a lu sur la "
        "facture, et ce dossier fusionne alors les deux tiers à la comptabilisation (définitif). À vérifier par le "
        "cabinet dans Experio, puis dites-moi de la comptabiliser."
    )


def _blocking(invoice_id, context):
    """Return what Experio's own check (the Comptabiliser button's) says blocks the invoice, as plain-text strings.

    Asks again up to RETRIES times, PAUSE seconds apart, while the list is not empty: the check can lag right after a
    refresh.
    """
    for attempt in range(RETRIES + 1):
        found = blocking(invoice_id, context)
        if not found or attempt == RETRIES:
            return found
        time.sleep(PAUSE)


@step_tool
def post_invoice(invoice_id: int) -> dict:
    """Post (comptabiliser) a draft invoice when Experio's own check finds nothing blocking.

    Returns a stop result listing what blocks it otherwise: a tiers merge only the cabinet can decide, transit
    disbursements not posted yet, a missing Compte Tiers, Experio's own blocking messages, assets without a category,
    a confirmation window Experio opens instead of posting, or Experio's refusal. An already posted invoice is a
    routine result.
    """
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id", "type", "partner_id", "line_ids"])
    if invoice["state"] in POSTED:
        return routine(f"Facture {invoice_id} : déjà comptabilisée.")
    if invoice["state"] != "draft":
        return stop(f"Facture {invoice_id} : en état {invoice['state']}, elle ne peut pas être comptabilisée.")
    merged = auto_merge(invoice)
    if merged:
        return merged
    first = disbursements_first(invoice)
    if first:
        return first
    role = missing_compte_tiers(invoice)
    if role:
        return stop(
            f"Facture {invoice_id} : pas comptabilisée, son tiers n'a pas de Compte Tiers {role} : le cabinet doit "
            "l'attribuer dans Experio (fiche du tiers), puis me dire de la comptabiliser."
        )
    context = invoice_context(invoice)
    with refused(f"post_invoice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier la facture."):
        # Experio's refresh rebuilds the writing lines: one the cabinet fixed by hand in Experio would be lost.
        if not invoice["line_ids"]:
            refresh(invoice_id, context)
        found = _blocking(invoice_id, context)
    if found:
        return stop(f"Facture {invoice_id} : pas comptabilisée, Experio signale : " + " ; ".join(found))
    with refused(f"post_invoice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier la facture."):
        assets = assets_without_category(invoice_id, context)
    if assets:  # Experio would post it and silently make no asset
        return stop(f"Facture {invoice_id} : pas comptabilisée : {assets_text(assets)}.")
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id} : rien ne bloque, elle serait comptabilisée.")
    try:
        # skip_due_date_detected_check: what the "date d'echeance detectee" popup's Confirmer does; over RPC the popup
        # would come back instead and nothing would post.
        action = odoo_json(
            "exp.invoice", "action_post", [invoice_id], context={**context, "skip_due_date_detected_check": True}
        )
        action = with_fiscal_year(action, {**context, "skip_due_date_detected_check": True})  # already confirmed above
        after = odoo_call("exp.invoice", "read", [invoice_id], fields=["state", "error_msg"], context=context)[0]
    except Exception as error:  # not refused(): a refusal here stops this invoice, with Experio's own reason
        log(f"post_invoice {invoice_id}: {error}")
        return stop(f"Facture {invoice_id} : Experio a refusé la comptabilisation : {experio_refusal(error)}")
    if after["state"] in POSTED:
        return ok(f"Facture {invoice_id} : comptabilisée.")
    # A window instead of the posting (a closed earlier exercice, a move's confirmation): over RPC nobody answers.
    if isinstance(action, dict) and action.get("res_model"):
        log(f"post_invoice {invoice_id}: action_post opened {action.get('res_model')}")
        return stop(
            f"Facture {invoice_id} : pas comptabilisée, Experio demande une confirmation à l'écran "
            f"(« {action.get('name') or action['res_model']} ») : à valider par le cabinet dans Experio."
        )
    return stop(f"Facture {invoice_id} : pas comptabilisée ({plain(after['error_msg']) or 'état ' + after['state']}).")
