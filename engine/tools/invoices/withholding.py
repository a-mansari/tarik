"""Withholding tax (retenue à la source, RAS).

Experio remembers each tiers' RAS: it saves the rule and rates on the tiers when an invoice is posted
(memorize_partner_values) and applies them when the tiers is picked in the form, so nothing is written onto the tiers
here. Experio also sets the RAS TVA rate itself once the retenue is on (_onchange_tax_withholding_percentage, an
onchange on partner_id, retenue and amount_total, in exp_invoice.py), from the supplier's activity type and
attestation de régularité, the dossier and the amount (the table in docs/experio-warnings.md): so Tarik never types
that rate. Left here: turning the retenue on for a local purchase whose supplier has an activity type (the cabinet's
rule, reported to the client afterwards), the supplier's attestation when Experio's rate depends on it (asked once
per supplier, its file uploaded to Experio), a foreign supplier's last posted rule, and the questions: a foreign
supplier with no known rule, local fees (6136) with no IS/IR rate.
"""

import base64

from engine.tools.core.app import ok, question, routine, step_tool, wait
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import POSTED, READING
from engine.tools.core.conversation import instructed
from engine.tools.core.experio import (
    account_code,
    dry_run,
    fill_invoice,
    invoice_context,
    odoo_call,
    odoo_json,
    refresh,
    refused,
    require_draft,
    require_invoice,
    test_mode_note,
)
from engine.tools.core.model import ask_model, to_pdf, tool_prompt
from engine.tools.core.queries import Q
from engine.tools.core.words import as_number, normalized, said

QUESTIONS = {
    "foreign": (
        "quelle retenue à la source étrangère : sans retenue, à la charge du fournisseur, "
        "ou à la charge de la société ? "
        "(ajoutez « hors Maroc » si le service est rendu à l'étranger)"
    ),
    "local": "y a-t-il une retenue à la source IS/IR ? Répondez « sans », ou le taux (ex. « 10 »).",
}


# The client's answer is read by the model, then checked here against the only values Experio takes:
# free text ("a la charge de la societe, contrat 2025", "sans, facture du 15/03") defeats any pattern.
FOREIGN_SHAPE = (
    'Answer with only a JSON object: {"rule": "none"|"supplier"|"society"|null, "abroad": true|false}. '
    "rule: none = sans retenue (1), supplier = à la charge du fournisseur (2), society = à la charge de la "
    "société (3), null when the answer does not say. abroad: the service was rendered outside Morocco "
    '("hors Maroc", "a l\'etranger").'
)
LOCAL_SHAPE = (
    'Answer with only a JSON object: {"retenue": true|false|null, "is_ir": 5|10|15|20|30|null, "tva": 0|75|100}. '
    'retenue: false for no withholding ("sans", "non"), true when the answer gives a rate, null when it does not '
    "say. is_ir: the IS/IR withholding rate it gives, else null. tva: the TVA withholding rate it gives, else 0."
)
IS_IR_RATES, TVA_RATES = {5, 10, 15, 20, 30}, {0, 75, 100}


def read_answer(kind, reply):
    """Return the model's reading (dict) of the client's ``reply`` to QUESTIONS[kind] (kind: "foreign" or "local")."""
    return ask_model(
        tool_prompt("read-withholding-answer"),
        [],
        f"Question: {QUESTIONS[kind]}\nClient's answer: {reply}\n\n"
        + (FOREIGN_SHAPE if kind == "foreign" else LOCAL_SHAPE),
        "Lecture de la réponse",
    )


def rate(value):
    """Return the whole rate the model gave (20, "20", "20 %", 20.0) as an int, None when it gave none.

    Raises:
        ValueError: a rate that is not whole, so the client is asked again rather than half of the answer applied.
    """
    number = as_number(value)
    if number is not None and number != int(number):
        raise ValueError(f"not a whole rate: {value!r}")
    return None if number is None else int(number)


def withholding_answer(reply):
    """Return the foreign rule the client chose in ``reply`` as Experio's fields, None when the answer does not say."""
    answer = read_answer("foreign", reply)
    rule = answer.get("rule")
    if rule not in ("none", "supplier", "society"):
        return None
    return {"retenue": rule != "none", "foreign_withholding": rule, "local_service": answer.get("abroad") is not True}


def local_withholding_answer(reply):
    """Return the local withholding the client gave in ``reply`` as Experio's fields, None when unclear or invalid.

    Either no withholding, or the retenue on with its IS/IR rate (5, 10, 15, 20, 30) and TVA rate (0, 75, 100).
    """
    answer = read_answer("local", reply)
    if answer.get("retenue") is False:
        return {"retenue": False, "local_withholding_percentage": False, "tax_withholding_percentage": "0"}
    try:
        is_ir = rate(answer.get("is_ir"))
        tva = rate(answer.get("tva", 0)) or 0
    except ValueError:
        return None
    if (
        answer.get("retenue") is not True
        or (is_ir is not None and is_ir not in IS_IR_RATES)
        or tva not in TVA_RATES
        or (is_ir is None and not tva)
    ):
        return None
    return {
        "retenue": True,
        "local_withholding_percentage": str(is_ir) if is_ir else False,
        "tax_withholding_percentage": str(tva),
    }


LOCAL_RAS_ACCOUNTS = ("6136",)  # honoraires, commissions: the local purchases where a RAS is usual
FIELDS = {
    "foreign": ("retenue", "foreign_withholding", "local_service"),
    "local": ("retenue", "local_withholding_percentage", "tax_withholding_percentage"),
}
RAS_TVA_FROM = "2024-07-01"  # Experio's RAS TVA applies from this invoice date (should_use_tax_withholding)
# The IS/IR rates Experio accepts per tiers type (validate_warning_local_withholding); any other type takes them all.
IS_IR_BY_TYPE = {"company": {5, 10, 20, 30}, "person": {10, 15, 20}}
NO_CERTIFICATE = ("sans attestation", "pas d attestation", "aucune attestation", "n a pas d attestation")
EXPENSE_LABEL = {
    "equipment": "équipement",
    "travaux": "travaux",
    "prestation": "prestation de services",
    "loyer": "loyer",
}


def described(values):
    """Return the withholding ``values`` (Experio's fields) in French words, for the client."""
    if not values.get("retenue"):
        return "sans retenue à la source"
    if "foreign_withholding" in values:
        payers = {"supplier": "à la charge du fournisseur", "society": "à la charge de la société"}
        who = payers.get(values["foreign_withholding"], "")
        abroad = "" if values.get("local_service", True) else ", service rendu hors Maroc"
        return f"retenue étrangère {who}{abroad}"
    rates = [f"IS/IR {values['local_withholding_percentage']} %"] if values.get("local_withholding_percentage") else []
    if values.get("tax_withholding_percentage") not in (None, False, "0"):
        rates.append(f"TVA {values['tax_withholding_percentage']} %")
    return "retenue à la source " + " et ".join(rates) if rates else "retenue à la source activée, à 0 %"


def _last_foreign_rule(invoice):
    """Return the foreign rule (Experio's fields) of the tiers' last posted foreign invoice, else None."""
    domain = [
        ("partner_id", "=", invoice["partner_id"][0]),
        ("id", "!=", invoice["id"]),
        ("state", "in", list(POSTED)),
        ("is_local_invoice", "=", False),
    ]
    last = odoo_call(
        "exp.invoice",
        "search_read",
        domain,
        fields=list(FIELDS["foreign"]),
        order="invoice_date desc, id desc",
        limit=1,
    )
    return {name: last[0][name] for name in FIELDS["foreign"]} if last else None


def certificate_matters(invoice_id, partner, public):
    """True when Experio's RAS TVA rate for this supplier depends on its attestation de régularité.

    Args:
        invoice_id: the invoice.
        partner: the supplier ({type, expense_type}).
        public: the dossier is a public organization (exp_is_public_organization).

    That is: equipment or works (company or person) outside a public dossier; a person's services over Experio's
    thresholds (should_use_service_regularity: TTC over 5 000, or this invoice plus the month's posted ones over
    50 000). Experio's rules: validate_warning_withholding_* in exp_invoice.py, lines 7623-8032.
    """
    expense, kind = partner["expense_type"], partner["type"]
    if expense in ("equipment", "travaux"):
        return not public and kind in ("company", "person")
    if expense == "prestation" and kind == "person":
        return bool(odoo_json("exp.invoice", "should_use_service_regularity", [invoice_id]))
    return False


def certificates(partner_id, day):
    """Return the state of supplier ``partner_id``'s attestations (exp.partner.regularity.form) at ``day``.

    Returns (valid, reading, refused):
        valid: one is valid at ``day`` as Experio's RAS check takes it (state active, regularity_date <= day <
            regularity_end_date).
        reading: one is still being read by Experio (bluejay_state in READING).
        refused: why Experio refused the latest one (its state_msg, when its state is error), else "".
    """
    fields = ["regularity_date", "regularity_end_date", "state", "bluejay_state", "state_msg"]
    domain = [("partner_id", "=", partner_id)]
    found = odoo_call("exp.partner.regularity.form", "search_read", domain, fields=fields, order="id desc")
    valid = any(
        r["state"] == "active" and r["regularity_date"] and r["regularity_date"] <= day < r["regularity_end_date"]
        for r in found
    )
    refused = " ".join((found[0]["state_msg"] or "").split()) if found and found[0]["state"] == "error" else ""
    return valid, any(r["bluejay_state"] in READING for r in found), refused


def no_certificate_said(partner_id):
    """True when the client already answered that supplier ``partner_id`` has no attestation (kept in Tarik's db)."""
    with cabinet_db() as db:
        return bool(db.execute(Q.no_certificate_of_partner, (partner_id,)).fetchone())


def upload_certificate(invoice, partner_id):
    """Upload the attestation the client sent after the question as the supplier's exp.partner.regularity.form.

    Experio then has Bluejay read it, as when the cabinet uploads it on the supplier's sheet. An image is turned into
    a PDF first. Returns the file's name (also in test mode, where nothing is uploaded), None when no PDF or image was
    sent.
    """
    with cabinet_db() as db:
        files = db.execute(Q.files_after_question, (invoice["id"],)).fetchall()
        sent = [f for f in files if f[2] == "application/pdf" or f[2].startswith("image/")]
        if not sent:
            return None
        file_id, name, mime, data = sent[0]
        if dry_run():
            return name
        raw = to_pdf(bytes(data)) if mime.startswith("image/") else bytes(data)
        values = {
            "partner_id": partner_id,
            "company_id": invoice["company_id"][0],
            "document_pdf": base64.b64encode(raw).decode(),
            "document_pdf_name": name,
        }
        regularity_id = odoo_call("exp.partner.regularity.form", "create", values, context=invoice_context(invoice))
        db.execute(Q.set_file_certificate, (regularity_id, file_id))
        db.execute(Q.forget_no_certificate, (partner_id,))
    return name


def certificate_step(invoice, partner, answer):
    """Get the supplier's attestation when Experio's RAS TVA rate depends on it and none is valid.

    ``answer``: the client's reply to the attestation question, None when the call does not answer it.

    Returns a wait result while Experio reads an attestation (one just sent is uploaded first), a question result
    asking for it, or None to go on: no RAS TVA at that date, the rate does not depend on it, a valid one exists, or
    the client said there is none (remembered for the supplier).
    """
    if (invoice["invoice_date"] or "") < RAS_TVA_FROM:  # no RAS TVA before it: nothing depends on an attestation
        return None
    public = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=["exp_is_public_organization"])[0]
    if not certificate_matters(invoice["id"], partner, public["exp_is_public_organization"]):
        return None
    valid, reading, refused = certificates(partner["id"], invoice["invoice_date"])
    if valid or no_certificate_said(partner["id"]):
        return None
    name = partner["name"]
    sent = upload_certificate(invoice, partner["id"]) if answer is not None else None
    if sent or reading:
        what = f"l'attestation « {sent} »" if sent else "l'attestation envoyée"
        return wait(
            f"Facture {invoice['id']} : {what} de `{name}` est en cours de lecture par Experio ; la retenue sera "
            "réglée dès qu'elle est lue (reprise à votre prochain message)."
        )
    if answer and any(said(words, normalized(answer)) for words in NO_CERTIFICATE):
        with cabinet_db() as db:
            db.execute(Q.note_no_certificate, (partner["id"],))
        return None
    why = f"L'attestation envoyée n'a pas été validée par Experio ({refused}). " if refused else ""
    activity = EXPENSE_LABEL.get(partner["expense_type"], "")
    return question(
        f"Facture {invoice['id']} : {why}{'L' if why else 'l'}a RAS TVA de `{name}` ({activity}) dépend de son "
        "attestation de "
        "régularité fiscale, que le dossier n'a pas (ou plus) en cours de validité. Envoyez-la en pièce jointe, ou "
        "répondez « sans attestation » (Experio retient alors la RAS TVA prévue sans attestation)."
    )


def is_ir_refused(values, kind):
    """Return the question text when the IS/IR rate in ``values`` is refused by Experio for tiers type ``kind``.

    None when the rate is accepted, or there is no rate or no rule for that type.
    """
    rate, rates = values.get("local_withholding_percentage"), IS_IR_BY_TYPE.get(kind)
    if not rate or not rates or int(rate) in rates:
        return None
    label = "une personne morale" if kind == "company" else "une personne physique"
    allowed = ", ".join(str(r) for r in sorted(rates))
    return f"le taux IS/IR de {rate} % ne vaut pas pour {label} (Experio prend {allowed} %) : lequel appliquer ?"


def _local(invoice, partner, answer):
    """Decide the local withholding to write (Experio's fields; the RAS TVA rate is left to Experio).

    Returns (result, None) when a question result comes first, or (None, values) with the values to write;
    (None, {}) when there is nothing to write. The retenue is on when the supplier has an activity type and the invoice
    is dated from RAS_TVA_FROM; fees accounts (6136) without a retenue are asked their IS/IR rate.
    """
    invoice_id, tier = invoice["id"], partner["name"]
    auto = bool(partner["expense_type"]) and (invoice["invoice_date"] or "") >= RAS_TVA_FROM
    values = {"retenue": True} if auto else {}
    if answer:
        read = local_withholding_answer(answer)
        if read is None:
            return question(f"Facture {invoice_id} : réponse non comprise, {QUESTIONS['local']}"), None
        read.pop("tax_withholding_percentage", None)  # Experio's rules set it, never the client's figure
        refusal = is_ir_refused(read, partner["type"])
        if refusal:
            return question(f"Facture {invoice_id} : {refusal}"), None
        if not read["retenue"] and auto:  # « sans » IS/IR: the retenue stays on for Experio's RAS TVA
            read = {"retenue": True, "local_withholding_percentage": False}
        return None, {**values, **read}
    if invoice["retenue"] or not account_code(invoice).startswith(LOCAL_RAS_ACCOUNTS):
        return None, values
    return question(f"Facture de `{tier}` : {QUESTIONS['local']}"), None


def _foreign(invoice, answer):
    """Decide the foreign rule to write (Experio's fields): from the client's ``answer``, else the tiers' last one.

    Returns (None, values), or (result, None) when a question result comes first (answer not understood, or no known
    rule).
    """
    if answer:
        values = withholding_answer(answer)
        if values is None:
            return question(f"Facture {invoice['id']} : réponse non comprise, {QUESTIONS['foreign']}"), None
        return None, values
    values = _last_foreign_rule(invoice)
    if values:
        return None, values
    return question(f"Facture de `{invoice['partner_id'][1]}` : {QUESTIONS['foreign']}"), None


@step_tool
def settle_withholding(invoice_id: int, answer: str = "", client_message: str = "", answering: bool = False) -> dict:
    """Settle a draft purchase invoice's withholding tax (RAS). Local: the retenue is turned on when the supplier
    has an activity type (Experio then sets the RAS TVA rate by its rules, asking first for the supplier's attestation
    when the rate depends on it), and fees (6136) are asked their IS/IR rate. Foreign: the tiers' known rule, else a
    question. Call again with the client's answer as ``answer`` (``answering``: it answers this step's question, a
    file they sent with it may be the supplier's attestation). With ``client_message``, ``answer`` is what the
    client wrote on the RAS with the invoice, taken only when the message holds it."""
    if client_message:
        answer = instructed(answer, client_message)
    fields = ["state", "type", "is_local_invoice", "company_id", "exercice_id", "partner_id", "account_id"]
    fields += ["invoice_date", *FIELDS["foreign"], *FIELDS["local"][1:]]
    invoice = require_invoice(invoice_id, fields)
    if not invoice["partner_id"]:
        return routine(f"Facture {invoice_id} : pas de retenue à la source à régler ici (tiers absent).")
    require_draft(invoice_id, invoice)
    if invoice["type"] == "sale":
        return sale_withholding(invoice, answer, answering)
    if not invoice["is_local_invoice"]:
        if invoice["retenue"]:  # the tiers' remembered rule, applied by Experio when the tiers was picked
            known = described({name: invoice[name] for name in FIELDS["foreign"]})
            return ok(f"Facture {invoice_id} : {known} (règle connue du tiers).")
        result, values = _foreign(invoice, answer)
        if result:
            return result
        return write(invoice, values)
    partner = odoo_call("exp.partner", "read", [invoice["partner_id"][0]], fields=["name", "type", "expense_type"])[0]
    waiting = certificate_step(invoice, partner, answer if answering else None)
    if waiting:
        return waiting
    if answer and any(said(words, normalized(answer)) for words in NO_CERTIFICATE):
        answer = ""  # the attestation question's answer, settled above
    result, values = _local(invoice, partner, answer)
    if result:
        return result
    if not values:
        if invoice["retenue"]:
            known = described({name: invoice[name] for name in FIELDS["local"]})
            return ok(f"Facture {invoice_id} : {known} (règle connue du tiers).")
        return routine(f"Facture {invoice_id} : sans retenue à la source.")
    automatic = "retenue" in values and not invoice["retenue"] and partner["expense_type"]
    why = f" (activité du fournisseur : {EXPENSE_LABEL[partner['expense_type']]})" if automatic else ""
    return write(invoice, values, why)


def sale_withholding(invoice, answer, answering):
    """Settle a sale's RAS TVA, which the customer holds, and return the step's result.

    Experio applies the same rules as for a purchase, from the customer's activity type and attestation
    (validate_warning_withholding; every sale is eligible, _is_tax_withholding_eligible_type). The retenue is turned
    on when the customer has an activity type, its rate left to Experio; else the customer's remembered rule, applied
    when it was picked, is reported.
    """
    invoice_id = invoice["id"]
    partner = odoo_call("exp.partner", "read", [invoice["partner_id"][0]], fields=["name", "type", "expense_type"])[0]
    waiting = certificate_step(invoice, partner, answer if answering else None)
    if waiting:
        return waiting
    if partner["expense_type"] and (invoice["invoice_date"] or "") >= RAS_TVA_FROM:
        automatic = "" if invoice["retenue"] else f" (activité du client : {EXPENSE_LABEL[partner['expense_type']]})"
        return write(invoice, {"retenue": True}, automatic)
    if invoice["retenue"]:
        known = described({name: invoice[name] for name in FIELDS["local"]})
        return ok(f"Facture {invoice_id} : {known} (règle connue du client).")
    return routine(f"Facture {invoice_id} : sans retenue à la source.")


def write(invoice, values, why=""):
    """Write ``values`` through Experio's form, then return an ok result saying what Experio holds.

    Experio's onchange sets the RAS lines, accounts and the RAS TVA rate. ``why``: added to the reply (the reason the
    retenue was turned on).
    """
    invoice_id, context = invoice["id"], invoice_context(invoice)
    with refused(
        f"settle_withholding {invoice_id}", f"Facture {invoice_id} : Experio a refusé la retenue à la source."
    ):
        changed = fill_invoice(invoice_id, values, context)
        refresh(invoice_id, context)
    kind = "foreign" if "foreign_withholding" in values else "local"
    held = {name: changed.get(name, values.get(name, invoice.get(name))) for name in FIELDS[kind]}
    return ok(f"{test_mode_note()}Facture {invoice_id} : {described(held)}{why}.")
