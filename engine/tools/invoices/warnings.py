"""Experio's warnings on an invoice, each handled on its own -- fixed, waited out, dismissed, or asked about.

Experio posts despite its warnings (action_post raises on settings and errors only), but each warning says something
is off. Its nine checks (exp_invoice.py collect_validation_messages, the list in docs/experio-warnings.md) are called
here one by one: called alone, a check's refusal rolls back what it writes, so nothing is left on the invoice. What
Experio can fix itself is fixed its way (its onchange sets the RAS TVA rate), what time settles is waited for (an
exchange rate not published yet), what cannot apply to the invoice is dismissed as the cabinet would (action_dismiss_
warning, in the invoice's history), and only a real choice is asked.
"""

import calendar
import re
from datetime import date

from engine.tools.core.app import ok, question, routine, step_tool, stop, wait
from engine.tools.core.config import DOSSIER_DOMAIN, TEST_MODE
from engine.tools.core.conversation import kept_yes_no, yes_no
from engine.tools.core.experio import (
    dry_run,
    fill_invoice,
    invoice_context,
    odoo_call,
    odoo_json,
    refresh,
    refused,
    require_invoice,
    warns,
)
from engine.tools.invoices.accounts import set_account
from engine.tools.invoices.exercice import settle_exercice

FIELDS = [
    "state",
    "type",
    "company_id",
    "exercice_id",
    "partner_id",
    "invoice_date",
    "is_local_invoice",
    "amount_tax_non_recoverable",
    "amount_untaxed",
    "amount_tva",
    "retenue",
    "local_withholding_percentage",
    "tax_withholding_percentage",
    "detected_supplier_ice",
    "detected_supplier_if",
    "detected_customer_ice",
    "detected_customer_if",
    "dismissed_warning_codes",
]
# Experio's checks, the exercice first (the others read the invoice's dates and rates).
CHECKS = (
    "validate_warning_previous_charges",
    "validate_warning_withholding",
    "validate_warning_local_withholding",
    "validate_warning_use_prorata",
    "validate_warning_account_id",
    "validate_warning_invalid_ice_and_if",
    "validate_folder_dum_detection",
    "validate_end_of_month_exchange_rate",
    "validate_warning_today_exchange_rate",
)
PRORATA = "prorata_vat_rate_not_applied_on_invoice"
# The IS/IR rates Experio accepts per tiers type (exp_invoice.py validate_warning_local_withholding).
IS_IR_RATES = {"company": ("5", "10", "20", "30"), "person": ("10", "15", "20")}
TYPE_LABEL = {"company": "une personne morale", "person": "une personne physique"}
ROUNDS = 3  # each round fixes what it can, then everything is checked again


def dismissed(invoice):
    return set(filter(None, (invoice["dismissed_warning_codes"] or "").split(",")))


def ice_case(invoice, company):
    """Which part of Experio's ICE/IF check speaks (validate_warning_invalid_ice_and_if): ("reverse", code) when
    the issuer read is the dossier itself, ("mismatch", code) when the buyer read is another company, else None."""
    reverse = "supplier" if invoice["type"] == "purchase" else "customer"
    buyer = "customer" if invoice["type"] == "purchase" else "supplier"
    for number, field in (("ice", "ice"), ("if", "vat")):
        if company[field] and invoice[f"detected_{reverse}_{number}"] == company[field]:
            return "reverse", f"company_{number}_matches_detected_reverse_partner_{number}"
    for number, field in (("ice", "ice"), ("if", "vat")):
        read = invoice[f"detected_{buyer}_{number}"]
        if company[field] and read and read != company[field]:
            return "mismatch", f"detected_{number}_mismatch_with_company_record"
    return None


def dismiss(invoice, code, message, context):
    """Dismiss warning ``code`` as the cabinet does on the invoice form: Experio records who and when, and writes
    « Warning deactivated » in the invoice's history."""
    if dry_run():
        return
    odoo_call("exp.invoice", "write", [invoice["id"]], {"warning_code": code, "warning_msg": message}, context=context)
    odoo_json("exp.invoice", "action_dismiss_warning", [invoice["id"]], context=context)


def tiers_type(invoice):
    if not invoice["partner_id"]:
        return ""
    return odoo_call("exp.partner", "read", [invoice["partner_id"][0]], fields=["type"])[0]["type"]


def company_of(invoice):
    fields = ["name", "ice", "vat", "prorata_rate"]
    return odoo_call("res.company", "read", [invoice["company_id"][0]], fields=fields)[0]


def raised(invoice, context):
    """{check: Experio's message} for each of its checks that warns about the invoice, those the cabinet (or Tarik)
    dismissed on it left out."""
    found = {check: message for check in CHECKS if (message := warns(invoice["id"], check, context))}
    gone = dismissed(invoice)
    if "validate_warning_use_prorata" in found and PRORATA in gone:
        del found["validate_warning_use_prorata"]
    if "validate_warning_invalid_ice_and_if" in found:
        case = ice_case(invoice, company_of(invoice))
        if case and case[1] in gone:
            del found["validate_warning_invalid_ice_and_if"]
    return found


def rate_question(invoice, kind):
    rates = IS_IR_RATES[kind]
    return question(
        f"Facture {invoice['id']} : la retenue IS/IR de {invoice['local_withholding_percentage']} % ne vaut pas pour "
        f"{TYPE_LABEL[kind]} (Experio n'accepte que {', '.join(rates)} %). Quel taux appliquer ? Répondez le taux, "
        "ou « sans »."
    )


def handle(check, invoice, message, context):
    """What is done about one warning: a phrase for the client when it was fixed, else the result that ends this
    step (a question, a wait, a stop)."""
    invoice_id = invoice["id"]
    if check == "validate_warning_previous_charges":
        result = settle_exercice(invoice_id)
        return result["message"] if result["status"] == "ok" else result
    if check == "validate_warning_withholding":
        # The TVA amount missing (missing_vat_amount_for_ras): the invoice's figures, not a rate.
        if invoice["amount_tax_non_recoverable"] and not invoice["amount_tva"] and invoice["retenue"]:
            return stop(f"Facture {invoice_id} : {message}")
        # Experio's own onchange sets the rate its rules give (the tiers' activity type, regularity, amount).
        changed = fill_invoice(invoice_id, {"retenue": True}, context)
        refresh(invoice_id, context)
        rate = changed.get("tax_withholding_percentage", invoice["tax_withholding_percentage"])
        return f"RAS TVA mise à {rate} % selon les règles d'Experio pour ce fournisseur"
    if check == "validate_warning_local_withholding":
        kind = tiers_type(invoice)
        if kind not in IS_IR_RATES or not invoice["local_withholding_percentage"]:
            return stop(f"Facture {invoice_id} : {message}")  # the HT amount is missing: the invoice's figures
        return rate_question(invoice, kind)
    if check == "validate_warning_use_prorata":
        settable = invoice["type"] == "purchase" and invoice["is_local_invoice"]
        if settable and not invoice["amount_tax_non_recoverable"]:
            fill_invoice(invoice_id, {"tax_prorata": True}, context)
            refresh(invoice_id, context)
            return f"prorata de TVA du dossier appliqué ({company_of(invoice)['prorata_rate']} %)"
        # The form shows no Prorata box on a sale, a foreign purchase or non-recoverable TVA: nothing to tick.
        dismiss(invoice, PRORATA, message, context)
        return "avertissement de prorata levé : le prorata ne s'applique pas à cette facture"
    if check == "validate_warning_account_id":
        return question(f"Facture {invoice_id} : Experio signale : {message} Quel compte utiliser (code ou intitulé) ?")
    if check == "validate_warning_invalid_ice_and_if":
        return ice_warning(invoice, message, context)
    if check == "validate_folder_dum_detection":
        return stop(f"Facture {invoice_id} : Experio signale : {message} À régler par le cabinet (dossier d'import).")
    if check == "validate_end_of_month_exchange_rate":
        day = date.fromisoformat(invoice["invoice_date"])
        end = day.replace(day=calendar.monthrange(day.year, day.month)[1])
        return wait(
            f"Facture {invoice_id} : le taux de change moyen du mois n'est définitif qu'après le {end:%d/%m/%Y} ; "
            "laissée en brouillon, reprise à votre premier message après cette date."
        )
    # validate_warning_today_exchange_rate
    return wait(
        f"Facture {invoice_id} : datée d'aujourd'hui, le taux de change du jour n'est pas encore publié (Experio a "
        "pris celui de la veille) ; laissée en brouillon, reprise à votre message de demain."
    )


def ice_warning(invoice, message, context):
    """The ICE/IF warning: a sale read as a purchase stops the invoice; a buyer that is not the dossier is asked about
    once (oui: kept in this dossier, the warning dismissed)."""
    invoice_id, company = invoice["id"], company_of(invoice)
    case, code = ice_case(invoice, company) or ("", "")
    if case == "reverse":
        return stop(
            f"Facture {invoice_id} : l'émetteur lu sur la facture porte l'ICE ou l'IF du dossier `{company['name']}` : "
            "c'est sans doute une facture de vente, pas d'achat. Laissée en brouillon, rien n'a été modifié."
        )
    _, kept = kept_yes_no(invoice_id, "settle_warnings", "")
    if kept:
        dismiss(invoice, code, message, context)
        return "ICE de l'acheteur différent de celui du dossier : gardée dans ce dossier, comme vous l'avez dit"
    if kept is False:
        return stop(f"Facture {invoice_id} : pas pour le dossier `{company['name']}`, laissée en brouillon.")
    buyer = "customer" if invoice["type"] == "purchase" else "supplier"
    number = invoice[f"detected_{buyer}_ice"] or invoice[f"detected_{buyer}_if"]
    domain = [("ice", "=", number), ("id", "!=", company["id"]), *DOSSIER_DOMAIN]
    others = odoo_call("res.company", "search_read", domain, fields=["name"], limit=2) if number else []
    elsewhere = f" C'est l'ICE du dossier `{others[0]['name']}`." if len(others) == 1 else ""
    return question(
        f"Facture {invoice_id} : elle est adressée à l'ICE/IF {number}, pas à celui du dossier `{company['name']}`."
        f"{elsewhere} Est-elle bien pour le dossier `{company['name']}` ? oui / non"
    )


@step_tool
def settle_warnings(invoice_id: int) -> dict:
    """Handle each warning Experio gives on a draft invoice before it is posted: fixed when Experio or Tarik can (the
    RAS TVA rate by Experio's rules, the dossier's prorata, the exercice), waited out when time settles it (an exchange
    rate not published yet), dismissed when it cannot apply, asked about only when it is a real choice."""
    invoice = require_invoice(invoice_id, FIELDS)
    if invoice["state"] != "draft":
        return routine(f"Facture {invoice_id} : en état {invoice['state']}, rien à régler.")
    context = invoice_context(invoice)
    done, tried = [], set()
    with refused(f"settle_warnings {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier la facture."):
        for _ in range(ROUNDS):
            found = raised(invoice, context)
            if not found:
                break
            if dry_run():
                listed = " ; ".join(found.values())
                return ok(f"{TEST_MODE}Facture {invoice_id} : avertissements d'Experio à régler : {listed}")
            for check, message in found.items():
                if check in tried:  # fixed once already and still there: Experio's own words to the client
                    return stop(f"Facture {invoice_id} : Experio signale toujours : {message}")
                tried.add(check)
                result = handle(check, invoice, message, context)
                if isinstance(result, dict):
                    return with_done(result, done)
                done.append(result)
            invoice = require_invoice(invoice_id, FIELDS)
            context = invoice_context(invoice)  # its exercice may have changed: Experio checks against it
        else:
            found = raised(invoice, context)
            if found:
                return stop(f"Facture {invoice_id} : Experio signale toujours : " + " ; ".join(found.values()))
    if not done:
        return routine(f"Facture {invoice_id} : aucun avertissement d'Experio.")
    return ok(f"Facture {invoice_id} : " + " ; ".join(done) + ".")


def with_done(result, done):
    """``result``, telling first what was fixed before it."""
    if not done:
        return result
    head, sep, rest = result["message"].partition(" : ")
    return {**result, "message": f"{head} : {' ; '.join(done)}. {rest}" if sep else result["message"]}


def answer_warnings(invoice_id, answer, decision=""):
    """The client's answer to settle_warnings' question, applied to the warning that asked it (found again, Experio's
    checks being the truth), then the remaining warnings handled."""
    invoice = require_invoice(invoice_id, FIELDS)
    context = invoice_context(invoice)
    with refused(f"settle_warnings {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier la facture."):
        found = raised(invoice, context)
    if "validate_warning_previous_charges" in found:
        result = settle_exercice(invoice_id, answer, decision)
        if result["status"] != "ok":
            return result
    elif "validate_warning_local_withholding" in found:
        kind = tiers_type(invoice)
        rates = IS_IR_RATES.get(kind, ())
        words = (answer or "").lower()
        given = [rate for rate in re.findall(r"\d+", words) if rate in rates]
        if "sans" in words.split():
            values = {"local_withholding_percentage": False}
        elif len(set(given)) == 1:
            values = {"local_withholding_percentage": given[0]}
        else:
            asked = rate_question(invoice, kind)
            return {**asked, "message": "Réponse non comprise. " + asked["message"]}
        fill_invoice(invoice_id, values, context)
        refresh(invoice_id, context)
    elif "validate_warning_account_id" in found:
        result = set_account(invoice_id, answer)
        if result["status"] != "ok":
            return result
    elif "validate_warning_invalid_ice_and_if" in found:
        told = yes_no(decision) if yes_no(decision) is not None else yes_no(answer)
        if told is None:
            return question(f"Réponse non comprise. Facture {invoice_id} : est-elle bien pour ce dossier ? oui / non")
        kept_yes_no(invoice_id, "settle_warnings", "oui" if told else "non")
    return settle_warnings(invoice_id)
