"""Post (« Comptabiliser ») a draft invoice, only when Experio's own check finds nothing blocking.

Everything goes through Experio's own methods: its refresh, collect_validation_messages (what the Comptabiliser
button checks), action_post, then the state read back, because action_post's return value says nothing reliable.
Posting also saves the invoice's account, non-recoverable TVA and RAS settings on its tiers for the next invoices
(exp.invoice.memorize_partner_values).
"""

import time
from collections import Counter

from engine.tools.core.app import CRASHED, log, ok, routine, step_tool, stop
from engine.tools.core.config import POSTED, TEST_MODE
from engine.tools.core.experio import (
    blocking,
    dry_run,
    experio_refusal,
    invoice_context,
    is_refusal,
    odoo_call,
    odoo_json,
    plain,
    refresh,
    refused,
    require_invoice,
)
from engine.tools.dossiers.progress import STATES
from engine.tools.invoices.check import stored_check
from engine.tools.invoices.classify import side_of
from engine.tools.invoices.configuration import assets_text, assets_without_category
from engine.tools.invoices.exercice import with_fiscal_year
from engine.tools.invoices.tiers import ensure_tiers_account

RETRIES, PAUSE = 3, 1.0  # the check can lag right after a refresh
BANK_LINE = "exp.bank.statement.line"
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

    Only for a transit invoice (invoice_type "transit") of an import folder. The invoices it paid are those of the
    folder marked « Payée par transitaire » (is_disbursement_invoice, by hand) and those matching one of its débours
    lines (paid_by_transit). Returns a stop result while some are still draft or pending, because Experio links only
    posted ones. Otherwise links the posted ones it lacks as a user picks them on its form (disbursement_invoice_ids:
    same folder, posted, not a transit invoice; not linked to another transit invoice, as
    _onchange_autofill_disbursement_invoices), refreshes the invoice so its writing gets one disbursement line per
    linked invoice, and returns a French note when part of its débours has no invoice of the folder behind it
    (unbacked_disbursement), else None. Returns None for any other invoice.
    """
    fields = ["invoice_type", "folder_id", "folder_type", "disbursement_invoice_ids", "company_id", "exercice_id"]
    fields.append("is_local_invoice")
    found = odoo_call("exp.invoice", "read", [invoice["id"]], fields=fields)[0]
    if found["invoice_type"] != "transit" or not found["folder_id"] or found["folder_type"] != "import":
        return None
    others = [("folder_id", "=", found["folder_id"][0]), ("invoice_type", "!=", "transit")]
    others += [("transit_invoice_id", "in", [False, invoice["id"]]), ("state", "!=", "cancel")]
    marked = odoo_call("exp.invoice", "search", [*others, ("is_disbursement_invoice", "=", True)])
    chosen = sorted({*marked, *paid_by_transit(invoice["id"], found, others)})
    folder = [("id", "in", chosen)]
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
    context = invoice_context(found)
    if missing and not dry_run():
        odoo_call(
            "exp.invoice",
            "write",
            [invoice["id"]],
            {"disbursement_invoice_ids": [(4, i) for i in missing]},
            context=context,
        )
        refresh(invoice["id"], context)  # its disbursement lines, one per linked invoice
    left = unbacked_disbursement(invoice["id"], context)
    if not left:
        return None
    none_marked = ""
    if not chosen:
        none_marked = (
            " (aucune facture du dossier ne correspond à ses lignes de débours ni n'est marquée « Payée par "
            "Transitaire » dans Experio)"
        )
    return (
        f"débours de {left:.2f} sans facture du dossier derrière (payée par le transitaire){none_marked}, passé au "
        "compte de débours : à justifier par le cabinet"
    )


def paid_by_transit(transit_id, transit, others, context=None):
    """Return the ids of the folder invoices (``others``: the folder's domain) a transit invoice paid, matched on its
    débours lines: each débours line Tarik read on its scan (check_invoice's debours_lines) takes the invoice whose
    total (display_amount_total) is its amount; an amount on as many lines as invoices takes them all, else none.

    Only in MAD and unpaid: Experio books a linked invoice's débours line at its total unconverted, and books no
    débours lines on a foreign transit invoice (generate_tva_line_values, exp_invoice.py:6390-6403); an invoice
    already paid would be settled twice. ``transit``: the transit invoice's is_local_invoice."""
    check = stored_check(transit_id)
    lines = (check[1].get("debours_lines") if check else None) or []
    debours = Counter(round(line["amount"], 2) for line in lines if (line.get("amount") or 0) > 0)
    if not debours or not transit["is_local_invoice"]:
        return []
    domain = [*others, ("is_local_invoice", "=", True), ("payment_state", "=", "unpaid")]
    invoices = odoo_call("exp.invoice", "search_read", domain, fields=["display_amount_total"], context=context)
    by_amount = {}
    for invoice in invoices:
        by_amount.setdefault(round(invoice["display_amount_total"], 2), []).append(invoice["id"])
    return [
        i for amount, count in debours.items() if len(by_amount.get(amount, [])) == count for i in by_amount[amount]
    ]


def unbacked_disbursement(invoice_id, context):
    """Return the part of a transit invoice's débours that no linked invoice covers: Experio books it on the
    dossier's débours account, on a disbursement line linked to no invoice (generate_tva_line_values, exp_invoice.py
    6415-6432)."""
    domain = [("invoice_id", "=", invoice_id), ("line_type", "=", "disbursement")]
    domain.append(("linked_disbursement_invoice_id", "=", False))
    lines = odoo_call("exp.invoice.line", "search_read", domain, fields=["amount_ht"], context=context)
    return round(sum(line["amount_ht"] for line in lines), 2)


def paid_before(invoice_id, context):
    """Tie invoice ``invoice_id``, just posted, to the one posted bank payment waiting for it: a purchase (or sale)
    operation of its tiers, of exactly its amount, with no invoice and unpaid (in suspense, as Experio's « Suspense »
    screen lists them). Writing its invoice_ids reconciles them, posted or not (bank_statement_line.py write ->
    reconcile_invoice_ids). Return a French phrase, "" when none or several (left to the cabinet). A credit note is
    left out (its payment goes the other way)."""
    fields = ["type", "refund", "partner_id", "amount_residual", "company_id"]
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=fields, context=context)[0]
    amount, way = round(invoice["amount_residual"], 2), "debit" if invoice["type"] == "purchase" else "credit"
    if invoice["refund"] or not invoice["partner_id"] or invoice["type"] not in ("purchase", "sale") or not amount:
        return ""
    domain = [("company_id", "=", invoice["company_id"][0]), ("state", "in", list(POSTED))]
    domain += [("operation_type", "=", invoice["type"]), ("partner_id", "=", invoice["partner_id"][0])]
    domain += [("invoice_ids", "=", False), ("payment_state", "=", "unpaid"), ("is_reconciled", "=", False)]
    domain.append((way, "=", amount))
    found = odoo_call(BANK_LINE, "search_read", domain, fields=["payment_ref", "date"], context=context)
    if len(found) != 1:
        return ""
    if not dry_run():
        odoo_call(BANK_LINE, "write", [found[0]["id"]], {"invoice_ids": [(4, invoice_id)]}, context=context)
    return f"rapprochée du paiement déjà comptabilisé « {found[0]['payment_ref']} » du {found[0]['date']}"


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
        state = STATES.get(invoice["state"], (invoice["state"],) * 2)[1]  # the feminine word, for a facture
        return stop(f"Facture {invoice_id} : elle est {state} dans Experio, elle ne peut donc pas être comptabilisée.")
    merged = auto_merge(invoice)
    if merged:
        return merged
    first = disbursements_first(invoice)
    if isinstance(first, dict):
        return first
    noted = f" Note : {first}." if first else ""
    role, told = missing_compte_tiers(invoice), ""
    if role:  # linked before its dossier had a series (a FEC loaded after): the dossier's series gives it now
        partner, kind, context = invoice["partner_id"][0], side_of(invoice), invoice_context(invoice)
        with refused(
            f"post_invoice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu attribuer le compte tiers."
        ):
            phrase = ensure_tiers_account(invoice_id, partner, kind, context)
        role = missing_compte_tiers(invoice)
        told = f" Son {phrase}." if not role else ""
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
        if not is_refusal(error):
            return stop(f"Facture {invoice_id} : {CRASHED}")
        return stop(f"Facture {invoice_id} : Experio a refusé la comptabilisation : {experio_refusal(error)}")
    if after["state"] in POSTED:
        try:  # posted already: a payment that cannot be tied stays the cabinet's, the posting stands
            tied = paid_before(invoice_id, context)
        except Exception as error:
            log(f"post_invoice {invoice_id}: paid_before: {error}")
            tied = ""
        tied = f" Elle est {tied}." if tied else ""
        return ok(f"Facture {invoice_id} : comptabilisée.{told}{noted}{tied}")
    # A window instead of the posting (a closed earlier exercice, a move's confirmation): over RPC nobody answers.
    if isinstance(action, dict) and action.get("res_model"):
        log(f"post_invoice {invoice_id}: action_post opened {action.get('res_model')}")
        window = f"la fenêtre « {action['name']} »" if action.get("name") else "une fenêtre de confirmation"
        return stop(
            f"Facture {invoice_id} : pas comptabilisée, Experio ouvre à l'écran {window} : à valider par le cabinet "
            "dans Experio."
        )
    why = (
        plain(after["error_msg"])
        or "elle est " + STATES.get(after["state"], (after["state"],) * 2)[1] + " dans Experio"
    )
    return stop(f"Facture {invoice_id} : pas comptabilisée ({why}).")
