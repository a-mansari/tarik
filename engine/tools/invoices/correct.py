"""Write what the scan shows over what Experio captured differently."""

from engine.tools.core.app import ToolError, log, ok, routine, step_tool, stop
from engine.tools.core.config import TEST_MODE
from engine.tools.core.conversation import instructed
from engine.tools.core.experio import (
    dry_run,
    fill_invoice,
    invoice_context,
    odoo_call,
    refresh,
    refused,
    require_draft_invoice,
)
from engine.tools.core.words import normalized
from engine.tools.invoices.check import CAPTURED, HOTEL, stored_check
from engine.tools.invoices.configuration import save_settings

MULTI_TVA_RATES = {7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20}  # the amount_tva_<rate> fields Experio has

PAYMENT_METHODS = {
    "especes": "Esp",
    "cheque": "Ch",
    "virement": "Virement",
    "effet": "Effet",
    "prelevement": "Pr",
    "compensation": "Compensation",
}


# How the client reads a corrected field; amount_tva_<rate> is « TVA <rate> % ». A field holding an id (mode de
# paiement, devise) is said set, its id meaning nothing to them; multi_tva is Experio's own switch, never said.
FIELD_LABELS = {
    "invoice_date": "date",
    "invoice_number": "numéro",
    "amount_untaxed": "montant HT",
    "amount_tva": "TVA",
    "amount_total": "montant TTC",
    "amount_tax_non_recoverable": "TVA non récupérable",
    "is_credit_note": "avoir",
    "payment_method_id": "mode de paiement",
    "currency_id": "devise",
    "amount_ht_restauration": "HT restauration",
    "amount_ht_breakfast": "HT petit-déjeuner",
    "amount_ht_blanchisserie": "HT blanchisserie",
    # Experio's own French labels (invoice/i18n/fr.po)
    "amount_stamp": "timbre",
    "amount_disbursement": "débours",
    "amount_guarantee_holdback": "retenue de garantie",
    "amount_prorata": "prorata",
    "amount_insurance": "assurance BTP",
    "amount_ts": "taxe de séjour",
    "amount_tsp": "taxe de promotion touristique",
    "advance_amount": "avance / acompte",
}


def change_text(field, value):
    """One corrected field as the client reads it (None: not said)."""
    if field == "multi_tva":
        return None
    if field == "refund":
        return "avoir" if value else "facture, pas un avoir"
    if field.startswith("amount_tva_") and field.removeprefix("amount_tva_").isdigit():
        return f"TVA {field.removeprefix('amount_tva_')} % {value}"
    label = FIELD_LABELS.get(field, field)
    return f"{label} renseigné(e)" if field.endswith("_id") else f"{label} {value}"


def titled_invoice(extracted):
    """True when the document's printed title (as read on the scan) says it is an invoice, never a credit note:
    « Facture », « Facture N° », without « avoir » nor « credit »."""
    title = normalized(extracted.get("document_title") or "")
    return "facture" in title.split() and not any(word in title for word in ("avoir", "credit", "rectificative"))


def planned_changes(extracted, bad_fields, invoice):
    """What the check says to write ({field: value}): the differing header fields, the credit note and
    the multi-TVA split. ``extracted`` and ``bad_fields`` come from the check, ``invoice`` holds refund and
    amount_tax_non_recoverable."""
    changes = {
        b["field"]: extracted[b["field"]]
        for b in bad_fields
        if b["field"] in CAPTURED and extracted.get(b["field"]) is not None  # the header: tiers, account... come later
    }
    if extracted.get("is_credit_note") is True and not invoice.get("refund"):  # the model's "false" is not true
        changes["refund"] = True
    elif invoice.get("refund") and extracted.get("is_credit_note") is False and titled_invoice(extracted):
        changes["refund"] = False  # Experio's reading ticked « avoir », the document says « Facture » in its title
    lines = [
        line for line in extracted.get("tva_lines") or [] if line.get("amount_ht") and line.get("rate") is not None
    ]
    rates = {}  # rate -> its TVA: an invoice may list several lines at one rate
    for line in lines:
        rates[int(line["rate"])] = round(rates.get(int(line["rate"]), 0) + (line.get("amount_tva") or 0), 2)
    # Split only from lines read in full that add up to the TVA read on the scan: a line not read, or read twice, never
    # becomes a written amount.
    trusted = (
        all(line.get("amount_tva") is not None for line in lines)
        and extracted.get("amount_tva") is not None
        and abs(sum(rates.values()) - extracted["amount_tva"]) <= 1
    )
    if (
        len(rates) > 1
        and trusted
        and not invoice.get("amount_tax_non_recoverable")
        and set(rates) - {0} <= MULTI_TVA_RATES
    ):
        changes["multi_tva"] = True
        changes.update({f"amount_tva_{rate}": amount for rate, amount in rates.items() if rate})
        changes["amount_tva"] = round(sum(rates.values()), 2)
    return changes


# A tax Experio counts in the TTC only when the dossier uses it (get_amounts_list: use_ts_tax, use_tsp_tax), and books
# on the dossier's account for it (get_amount_ts_account_id: no detection fills it, it has no default): the setting,
# its account, and how the client reads it.
TAX_SETTINGS = {
    "amount_ts": ("use_ts_tax", "ts_account_id", "taxe de séjour"),
    "amount_tsp": ("use_tsp_tax", "tsp_account_id", "taxe de promotion touristique"),
}


def tax_settings(invoice, changes, notes):
    """The tourist taxes the invoice has (or gets from ``changes``), when the dossier has their account: their setting
    turned on as its Configuration screen does (else Experio leaves them out of the TTC). Without the account, the
    amounts stay as Experio read them (the tax kept out of ``changes``, the stamp it was read as too) and the cabinet
    is told: the invoice would not post otherwise."""
    taxed = [field for field in TAX_SETTINGS if (changes.get(field, invoice.get(field)) or 0) > 0]
    if not taxed:
        return
    company_id, context = invoice["company_id"][0], invoice_context(invoice)
    wanted = [name for field in taxed for name in TAX_SETTINGS[field][:2]]
    held = odoo_call("res.company", "read", [company_id], fields=wanted, context=context)[0]
    for field in taxed:
        setting, account, label = TAX_SETTINGS[field]
        if not held[account]:
            if field in changes:
                changes.pop(field)
                changes.pop("amount_stamp", None)  # Experio read the tax as a stamp: it stays so, the TTC balanced
            notes.append(
                f"{label} laissée telle que lue par Experio : le dossier n'a pas de compte de {label} "
                "(à renseigner par le cabinet dans les paramètres)"
            )
        elif held[setting]:
            continue
        elif dry_run():
            notes.append(f"{TEST_MODE}{label} activée dans les paramètres du dossier")
        elif save_settings(company_id, {setting: True}, context):
            notes.append(f"{label} activée dans les paramètres du dossier (Experio la compte alors dans le TTC)")
        else:
            changes.pop(field, None)
            notes.append(f"{label} à activer par le cabinet dans les paramètres du dossier, laissée telle quelle")


def hotel_parts(extracted, invoice, changes):
    """A hotel bill's HT parts read on the scan, added to ``changes`` for the parts the dossier uses (should_use_ht_*,
    Experio's multi HT) and the invoice has none of yet; the HT then keeps only the rest, as Experio adds the parts to
    it (get_actual_amount_ht) and to its TTC check (get_amounts_list). Nothing when the parts exceed the HT."""
    parts = {
        field: extracted[field]
        for field in HOTEL
        if (extracted.get(field) or 0) > 0 and invoice[f"should_use_{field.removeprefix('amount_')}"]
    }
    if not parts or any(invoice[field] for field in HOTEL):
        return
    untaxed = changes.get("amount_untaxed", extracted.get("amount_untaxed") or invoice["amount_untaxed"] or 0)
    rest = round(untaxed - sum(parts.values()), 2)
    if rest >= 0:
        changes.update(parts)
        changes["amount_untaxed"] = rest


def _currency(code, changes, notes):
    """Add the currency ``code`` (ISO) the scan shows to ``changes``, or a note to ``notes`` when Experio lacks it."""
    if code == "MAD":
        return
    currency = odoo_call("res.currency", "search_read", [("name", "=", code)], fields=["id"], limit=1)
    if currency:
        changes["currency_id"] = currency[0]["id"]  # Experio fetches the rate for the invoice date itself
    else:
        notes.append(f"devise {code} inconnue dans Experio")


def _payment_method(name, invoice, changes):
    """Add the payment method ``name`` the scan shows (especes, cheque...) to ``changes``, when the invoice has none
    and Experio has it."""
    method = PAYMENT_METHODS.get((name or "").lower())
    if method and not invoice["payment_method_id"]:
        found = odoo_call(
            "exp.payment.method", "search_read", [("name", "=ilike", f"{method}%")], fields=["id"], limit=1
        )
        if found:
            changes["payment_method_id"] = found[0]["id"]


@step_tool
def correct_invoice(invoice_id: int, payment_method: str = "", client_message: str = "") -> dict:
    """Correct a draft invoice from its check: writes what check_invoice read on
    the scan over the fields Experio captured differently (amounts, number,
    date, credit note, multi-TVA, currency, payment method), then lets Experio
    rebuild its TVA lines. Call check_invoice first. A scan whose total is 0 or less (a payment's
    receipt) keeps the amounts Experio captured; stopped, nothing written, when Experio has none
    either. ``payment_method``: the one the client wrote with the invoice (especes, cheque, virement, effet,
    prelevement, compensation), over the scan's; taken only when ``client_message`` holds it."""
    fields = [
        "state",
        "company_id",
        "exercice_id",
        "refund",
        "amount_tax_non_recoverable",
        "payment_method_id",
        "amount_total",
        "amount_untaxed",
        *TAX_SETTINGS,
        *HOTEL,
        *(f"should_use_{field.removeprefix('amount_')}" for field in HOTEL),
    ]
    invoice = require_draft_invoice(invoice_id, [*fields, "currency_id"], "être corrigee")
    check = stored_check(invoice_id)
    if not check:
        raise ToolError(f"Facture {invoice_id} : pas encore vérifiée, appelez d'abord check_invoice.")
    readable, extracted, bad_fields = check
    if not readable:
        return ok(f"Facture {invoice_id} : scan illisible, rien à corriger.")
    notes = []
    if extracted.get("amount_total") is not None and extracted["amount_total"] <= 0:
        # A hotel's folio after payment, a receipt: nothing owed on the scan. Keep the amounts Experio captured,
        # if any (Experio refuses to record a total of 0).
        if (invoice.get("amount_total") or 0) <= 0:
            return stop(
                f"Facture {invoice_id} : le document ne porte aucun montant a payer (total "
                f"{extracted['amount_total']:.2f}) : c'est sans doute un justificatif de paiement, pas une facture. "
                "Rien n'a été comptabilisé ; envoyez la facture elle-même, avec ses montants."
            )
        extracted = {
            k: v
            for k, v in extracted.items()
            if not k.startswith("amount_") and k not in ("tva_lines", "is_credit_note")
        }
        notes.append("montants gardés tels que saisis par Experio, le scan ne porte aucun montant à payer")
    changes = planned_changes(extracted, bad_fields, invoice)
    hotel_parts(extracted, invoice, changes)
    with refused(f"correct_invoice {invoice_id}", f"Facture {invoice_id} : Experio a refusé la correction."):
        tax_settings(invoice, changes, notes)
        _currency((extracted.get("currency") or "MAD").upper(), changes, notes)
        told = instructed(payment_method, client_message)
        _payment_method(told or extracted.get("payment_method"), invoice, changes)
        noted = f" ({'; '.join(notes)})" if notes else ""
        if not changes:
            return routine(f"Facture {invoice_id} : rien à corriger.{noted}")
        context = invoice_context(invoice)
        changed = fill_invoice(invoice_id, changes, context)
        if not changed:
            return routine(f"Facture {invoice_id} : déjà a jour, rien à changer.")
        refresh(invoice_id, context)
    texts = (change_text(field, changed.get(field, value)) for field, value in changes.items())
    shown = ", ".join(text for text in texts if text) or "montants"
    other = sorted(set(changed) - set(changes))
    if other:  # what Experio recomputed by itself (rate, its own codes): for the team, not the client
        log(f"correct_invoice {invoice_id}: Experio also updated {', '.join(other)}")
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id}, correction prévue : {shown}.{noted}")
    return ok(f"Facture {invoice_id} : corrigée ({shown}).{noted}")
