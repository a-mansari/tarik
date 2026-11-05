"""TVA: recoverable or not, the cabinet's prorata, and the tax on each line.

Experio does most of it: picking the tiers in the form applies its memorized non-recoverable and prorata flags, and
its refresh picks the tax for each rate from the dossier's tax settings (or says which setting is missing). Left
here: the question on the usual non-recoverable accounts, the cabinet's prorata, and the dossier's tax for a rate it
has none for (taxes.py), made before the refresh that would refuse that rate.
"""

from engine.knowledge.cgnc import consult_cgnc
from engine.tools.core.app import ToolError, log, noted, ok, question, routine, step_tool, stop
from engine.tools.core.config import TEST_MODE
from engine.tools.core.conversation import keep_answer, kept_answer, kept_yes_no, yes_no
from engine.tools.core.experio import (
    account_code,
    dry_run,
    fill_invoice,
    invoice_context,
    odoo_call,
    refresh,
    refused,
    require_draft_invoice,
    test_mode_note,
)
from engine.tools.core.model import ask_model, tool_prompt
from engine.tools.core.words import dirhams
from engine.tools.invoices.check import stored_check
from engine.tools.invoices.classify import side_of
from engine.tools.invoices.correct import MULTI_TVA_RATES, PER_RATE, rates_read
from engine.tools.invoices.taxes import add_missing_taxes

# CGI art. 106: a confirmer par le comptable avant usage en production.
NON_RECOVERABLE_ACCOUNTS = ("6142", "6143", "61223", "2340")  # travel, receptions, fuel, passenger cars
# 61223 « Achats de combustibles » in Experio's chart (exp_account experio_history.py:477); 61251 is water, electricity.


def deductibility(invoice, context):
    """Return (non_recoverable, reason) for a purchase on one of NON_RECOVERABLE_ACCOUNTS: True or False as the model
    decides, None when it cannot (the client is asked then). The model is shown the dossier's activity as far as
    Experio holds it (its name, its « secteur d'activité »), what the invoice bills and the account, and the CGNC's
    own words on that account: is this expense necessary to this company's activity, so its TVA deductible, or not."""
    company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=["name", "partner_id"])[0]
    sector = ""
    try:
        sector = odoo_call("exp.partner", "read", [company["partner_id"][0]], fields=["activity_sector"])[0]
        sector = sector.get("activity_sector") or ""
    except Exception as error:  # the dossier's own partner may not be readable: the name still says what it does
        log(f"deductibility {invoice['id']}: {error}")
    check = stored_check(invoice["id"])
    designation = (check[1].get("designation") if check else "") or "(not read)"
    code = account_code(invoice)
    instruction = (
        f"Dossier (the company whose books these are): {company['name']}\n"
        f"Its activity as Experio holds it: {sector or 'not given'}\n"
        f"Supplier: {invoice['partner_id'][1] if invoice['partner_id'] else 'unknown'}\n"
        f"The invoice bills: {designation}\nAccount: {code}\n\nWhat the CGNC says on this account:\n"
        f"{consult_cgnc(code=code)}\n\n"
        'Answer with only a JSON object: {"non_recoverable": true | false | null, "reason": "one short French '
        'sentence"}'
    )
    answer = ask_model(tool_prompt("decide-tva-deductible"), [], instruction, "Récupération de la TVA")
    decided = answer.get("non_recoverable")
    return (decided if isinstance(decided, bool) else None), str(answer.get("reason") or "")


def _non_recoverable(invoice, answer):
    """Decide whether a purchase's TVA is non-recoverable, from the client's ``answer``, else from the model (the
    dossier's activity, what is billed, the CGNC), else by asking.

    Returns (question, changes, reason). question is a question result when the answer is not understood, or when
    there is no answer and the model cannot tell for an account in NON_RECOVERABLE_ACCOUNTS; else None. changes is the
    invoice values « non récupérable » sets, {} otherwise; reason is the model's, "" when the client decided.
    """
    told = yes_no(answer) if answer else None
    if answer and told is None:
        return (
            question(f"Facture {invoice['id']} : réponse non comprise, la TVA est-elle non récupérable ? oui / non"),
            {},
            "",
        )
    if told is None and account_code(invoice).startswith(NON_RECOVERABLE_ACCOUNTS):
        decided, reason = deductibility(invoice, invoice_context(invoice))
        if decided is not None:
            return None, ({"amount_tax_non_recoverable": True, "multi_tva": False} if decided else {}), reason
        tier = f"`{invoice['partner_id'][1]}`" if invoice["partner_id"] else "ce fournisseur"
        asked = f"Facture de {tier} : la TVA est-elle non récupérable (voyage, carburant, véhicule de tourisme) ? "
        return question(asked + "oui / non"), {}, ""
    return None, ({"amount_tax_non_recoverable": True, "multi_tva": False} if told else {}), ""


def _tax_lines(invoice_id, context):
    """Return the invoice's untaxed TVA lines as text, each "<HT> HT à <rate> % (<tax>)", once Experio rebuilt them."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["tva_line_ids"], context=context)[0]
    line_ids = invoice["tva_line_ids"]
    fields = ["line_type", "tax_id", "tax_rate", "amount_ht", "amount_tva"]
    lines = odoo_call("exp.invoice.line", "read", line_ids, fields=fields, context=context) if line_ids else []
    return [
        f"{line['amount_ht']} HT à {line['tax_rate']} % "
        f"({'`' + line['tax_id'][1] + '`' if line['tax_id'] else 'sans taxe'})"
        for line in lines
        if line["line_type"] == "untaxed"
    ]


MOROCCAN_RATES = (7, 10, 14, 20)  # the TVA rates in force (CGI art. 98-100)


def rate_mismatch(invoice_id):
    """Return (printed rate, HT, TVA) when the scan prints one TVA rate that its TVA is not of its HT (« 20 % » with a
    TVA of 650 on 3 611,11: 18 %); None when they agree, or the scan's TVA lines were not read in full."""
    check = stored_check(invoice_id)
    extracted = check[1] if check else {}
    lines, rates, trusted = rates_read(extracted)
    if len(rates) != 1 or not trusted:
        return None
    (rate,) = rates
    base, tva = round(sum(line["amount_ht"] for line in lines), 2), extracted["amount_tva"]
    if not rate or not base or abs(round(base * rate / 100, 2) - tva) <= 0.02:
        return None
    return rate, base, tva


def rate_settled(invoice, mismatch, answer, context):
    """Settle a scan whose printed rate is not its TVA's (rate_mismatch), asking the client each time, never deciding:
    first whether part of its HT bears no TVA (oui: Experio's split stays, that part at 0 %); if not, whether to apply
    the rate its amounts give when it is a Moroccan rate (oui: the TVA put on that rate), else the invoice is to
    correct. Returns (a question or stop result, None) while it is not settled, (None, note) once it is; the client's
    ``answer`` is read by the question it answers, never by the next one."""
    invoice_id = invoice["id"]
    rate, base, tva = mismatch
    actual = round(tva / base * 100, 2)
    legal = next((r for r in MOROCCAN_RATES if abs(actual - r) <= 0.05), None)
    shown = f"{actual:g}".replace(".", ",")
    facts = (
        f"Facture {invoice_id} : le taux imprimé est {rate} %, mais la TVA ({dirhams(tva)}) fait {shown} % du HT "
        f"({dirhams(base)})."
    )
    # « part of the HT at 0 % » only when the TVA is below the printed rate's, at a rate Experio splits on (correct.py)
    partial = tva < round(base * rate / 100, 2) and rate in MULTI_TVA_RATES
    part = kept_answer(invoice_id, "settle_tva_part") if partial else "non"
    if not part:
        if yes_no(answer) is None:
            taxed = round(tva * 100 / rate, 2)
            return question(
                f"{facts} Est-ce qu'une partie du HT n'est pas soumise à la TVA ({dirhams(taxed)} à {rate} % et "
                f"{dirhams(round(base - taxed, 2))} à 0 %) ? oui / non"
            ), None
        part, answer = answer, ""  # read: the next question waits for its own answer
        keep_answer(invoice_id, "settle_tva_part", part)
    if yes_no(part):
        return None, "une partie du HT à 0 %, comme vous l'avez dit"
    if legal is None:
        return stop(f"{facts} {shown} % n'est pas un taux marocain : la facture est à corriger, puis à renvoyer."), None
    applied = kept_answer(invoice_id, "settle_tva_rate")  # once given, never read again from a later answer
    told = yes_no(applied or answer)
    if told is None:
        return question(f"{facts} J'applique {legal} % ? oui / non"), None
    if not applied:
        keep_answer(invoice_id, "settle_tva_rate", answer)
    if not told:
        return stop(f"{facts} La facture est à corriger, puis à renvoyer."), None
    if not applied:  # written once, when the client says it
        fill_invoice(
            invoice_id, {"multi_tva": True, **dict.fromkeys(PER_RATE, 0.0), f"amount_tva_{legal}": tva}, context
        )
    return None, f"taux de {legal} % appliqué à votre demande (le taux imprimé était {rate} %)"


@step_tool
def settle_tva(invoice_id: int, answer: str = "") -> dict:
    """Settle a draft invoice's TVA once its account is set: recoverable or not, prorata, and each line's tax.

    A purchase on a usual non-recoverable account is decided from the dossier's activity, what is billed and the CGNC
    (deductibility: shown to the client under « À vérifier »), asked only when the model cannot tell; a sale billing no
    TVA gets « TVA non facturée »;
    a purchase of a dossier using a TVA prorata gets it. The dossier's missing taxes are added, then Experio's refresh
    picks each line's tax. When the result is a question (non-recoverable TVA?), ask the client and call again with
    ``answer`` "oui" (non-recoverable) or "non", as the agent read their reply.

    Raises:
        ToolError: the invoice has no account yet (outside test mode).
    """
    fields = ["state", "type", "company_id", "exercice_id", "partner_id", "account_id", "amount_tva", "amount_total"]
    invoice = require_draft_invoice(invoice_id, [*fields, "amount_tax_non_recoverable", "tax_prorata", "tva_line_ids"])
    if not invoice["account_id"]:
        if dry_run():  # the account step only simulated its write
            return ok(f"{TEST_MODE}Facture {invoice_id} : la TVA sera réglée une fois le compte enregistré.")
        raise ToolError(
            f"Facture {invoice_id} : pas encore de compte, appelez d'abord assign_account (la TVA en dépend)."
        )
    purchase = side_of(invoice) == "purchase"
    context = invoice_context(invoice)
    changes, notes, reason = {}, [], ""
    mismatch = rate_mismatch(invoice_id)
    if mismatch:  # the printed rate is not the TVA's: asked, never decided (answered here, never read again below)
        with refused(f"settle_tva {invoice_id}", f"Facture {invoice_id} : Experio a refusé le taux de TVA."):
            stopped, note = rate_settled({**invoice, "id": invoice_id}, mismatch, answer, context)
        if stopped:
            return stopped
        notes.append(note)
        answer = ""
    if purchase and not invoice["amount_tax_non_recoverable"] and invoice["amount_tva"]:
        answer, _ = kept_yes_no(invoice_id, "settle_tva", answer)  # asked before: its answer, not the question again
        asked, changes, reason = _non_recoverable(invoice, answer)
        if asked:
            return asked
    # A sale billing no TVA (exonérée, hors champ): Experio's « TVA non facturée », one line at the TTC on its 0 %
    # sale tax (generate_tva_line_values), as the cabinet ticks it.
    no_tva = not invoice["amount_tax_non_recoverable"] and not invoice["amount_tva"]
    if not purchase and no_tva and (invoice["amount_total"] or 0) > 0:
        changes = {"amount_tax_non_recoverable": True, "multi_tva": False}
    non_recoverable = invoice["amount_tax_non_recoverable"] or changes.get("amount_tax_non_recoverable")
    with refused(f"settle_tva {invoice_id}", f"Facture {invoice_id} : Experio a refusé la mise à jour de la TVA."):
        if purchase and not non_recoverable and invoice["amount_tva"] and not invoice["tax_prorata"]:
            fields = ["use_prorata", "prorata_rate"]
            company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=fields)[0]
            if company["use_prorata"]:  # the dossier uses a TVA prorata
                changes["tax_prorata"] = True  # Experio's onchange applies the cabinet's rate
                notes.append(f"prorata de TVA {company['prorata_rate']} %")
        if changes:
            fill_invoice(invoice_id, changes, context)
            if changes.get("amount_tax_non_recoverable"):  # Experio saves it on the tiers when the invoice is posted
                notes.append("TVA non récupérable" if purchase else "TVA non facturée")
        taxes = add_missing_taxes(invoice_id, context)  # a rate without its tax: Experio's refresh would refuse it
        refresh(invoice_id, context)  # Experio picks the tax for each rate
        taxed = _tax_lines(invoice_id, context)
    # Only the TVA is said here: what else Experio refuses on the invoice is configure_cabinet's and post_invoice's.
    kind = ("non récupérable" if purchase else "non facturée") if non_recoverable else "récupérable"
    body = f"{test_mode_note()}Facture {invoice_id} : TVA {kind if purchase or non_recoverable else 'facturée'}"
    if notes and not non_recoverable:
        body += f", {', '.join(notes)}"
    body += ". "
    if taxed:
        body += "Lignes : " + "; ".join(taxed) + "."
    else:
        body += "Pas encore de ligne de TVA."
    if taxes:
        body += " Taxes du dossier : " + " ; ".join(taxes) + "."
    if reason:  # Tarik decided alone, from the dossier's activity and the CGNC: the client checks it
        verdict = "non récupérable" if non_recoverable else "récupérable"
        return noted(f"{body}Décidé par Tarik ({verdict}) : {reason} Dites-moi si c'est l'inverse.")
    told = non_recoverable or notes or taxes  # what the client should know: else routine
    return ok(body) if told else routine(body)
