"""The dossier's settings posting needs, filled as Experio's own screens fill them.

Before posting, Experio says which setting is missing (collect_validation_messages: the journal and the
tiers account of local or foreign purchases and sales, the stamp, debours, BTP and withholding accounts, the
tax of the invoice's rate...). Its Configuration screen has a "Detection automatique" button
(res.company.auto_detect_settings) that fills every unset setting from the chart of accounts and the journals,
by their codes; and its chart of accounts makes the taxes its TVA accounts call for (exp.account.create_tax_ids:
one tax per rate and type from the 3455 and 4455 accounts, the 0 % ones always). This step runs those two, then
turns on the settings the detection leaves off (a switch is never auto-detected): an amount the invoice carries
(retenue de garantie, prorata, assurance, avance, timbre, debours, taxes de sejour) that Experio would drop silently
with its switch off, with its account by its CGNC code when the detection found none. Then it reads again what
still blocks, told apart: a setting the dossier lacks (Experio's "cgnc" level, an account to choose) is the
cabinet's, in Configuration; what Experio refuses on the invoice itself (an amount, a field, a warning) is no
setting at all. Either stops the invoice there, before it is said verified.
"""

from tarik.core.app import ok, routine, step_tool, stop
from tarik.core.config import TEST_MODE
from tarik.core.experio import (
    blocking,
    checked,
    dry_run,
    invoice_context,
    odoo_call,
    odoo_json,
    refused,
    require_invoice,
)

TVA_ACCOUNTS = ["|", ("code", "=like", "3455%"), ("code", "=like", "4455%")]
# Per amount of the invoice: its name, then per side (purchase, sale) the dossier's switch (None: none), its
# account, and the CGNC code prefix(es) the account is found by when the detection left it empty (None: the
# cabinet's choice). Experio ignores a foreign purchase's deductions, and a foreign advance on both sides.
AMOUNTS = {
    "amount_guarantee_holdback": (
        "retenue de garantie",
        ("use_supplier_guarantee_holdback", "supplier_guarantee_holdback_account_id", ("4413",)),
        ("use_customer_guarantee_holdback", "customer_guarantee_holdback_account_id", ("3423",)),
    ),
    "amount_prorata": (
        "prorata",
        ("use_supplier_prorata", "supplier_prorata_account_id", ("6149",)),
        ("use_customer_prorata", "customer_prorata_account_id", ("787",)),
    ),
    "amount_insurance": (
        "assurance BTP",
        ("use_supplier_insurance", "supplier_insurance_account_id", ("6134",)),
        ("use_customer_insurance", "customer_insurance_account_id", ("634", "6134")),
    ),
    "advance_amount": (
        "avance / acompte",
        ("use_supplier_advance_account", "supplier_advance_account_id", ("3411",)),
        ("use_client_advance_account", "client_advance_account_id", ("4421",)),
    ),
    "amount_stamp": ("timbre", (None, "stamp_account_id", ("6167",)), (None, "stamp_sale_account_id", ("6167",))),
    "amount_disbursement": (
        "debours",
        (None, "disbursement_account_id", ("4488",)),
        (None, "disbursement_sale_account_id", ("3488",)),
    ),
    "amount_ts": ("taxe de sejour", (None, None, None), ("use_ts_tax", "ts_account_id", None)),
    "amount_tsp": ("taxe de promotion touristique", (None, None, None), ("use_tsp_tax", "tsp_account_id", None)),
}
FOREIGN_IGNORED = {"amount_guarantee_holdback", "amount_prorata", "amount_insurance", "advance_amount"}
INVOICE_FIELDS = ["state", "company_id", "exercice_id", "type", "is_local_invoice", *AMOUNTS]


def listed(texts):
    """``texts`` as one sentence: "a ; b." (Experio's own end with their period: not doubled)."""
    return " ; ".join(text.rstrip(". ") for text in texts) + "."


def tax_count(company_id):
    return odoo_call("exp.tax", "search_count", [("company_id", "=", company_id)])


def account_by_code(company_id, prefixes):
    """The id of the dossier's first account whose code starts with one of ``prefixes``, by code; None."""
    for prefix in prefixes:
        found = odoo_call(
            "exp.account",
            "search_read",
            [("company_id", "=", company_id), ("code", "=like", f"{prefix}%")],
            fields=["id"],
            order="code",
            limit=1,
        )
        if found:
            return found[0]["id"]
    return None


def ignored_abroad(amount, purchase):
    """True when Experio ignores ``amount`` on a foreign invoice: the stamp, a purchase's deductions, an advance."""
    if amount == "amount_stamp":
        return True
    return amount in FOREIGN_IGNORED and (purchase or amount == "advance_amount")


def amount_fields():
    """The dossier's switch and account fields AMOUNTS names, sorted."""
    fields = set()
    for _, *sides in AMOUNTS.values():
        for switch, account, _ in sides:
            fields |= {field for field in (switch, account) if field}
    return sorted(fields)


def settings_needed(invoice, company):
    """What the invoice's amounts need the dossier to have and it hasn't: ({company field: value}, [names set],
    [names whose account only the cabinet can choose])."""
    purchase, foreign = invoice["type"] == "purchase", not invoice["is_local_invoice"]
    values, names, cabinet = {}, [], []
    for amount, (name, supplier, customer) in AMOUNTS.items():
        switch, account, prefixes = supplier if purchase else customer
        ignored = foreign and ignored_abroad(amount, purchase)
        if not invoice[amount] or not account or ignored:
            continue
        if not company[account]:
            found = prefixes and account_by_code(invoice["company_id"][0], prefixes)
            if not found:
                cabinet.append(name)
                continue
            values[account] = found
        if switch and not company[switch]:
            values[switch] = True
        if account in values or switch in values:
            names.append(name)
    return values, names, cabinet


def save_settings(company_id, values, context):
    """Write ``values`` as the Configuration screen does: a res.config.settings form, whose fields are the
    dossier's. True when the dossier now holds them (the cabinet's rights decide)."""
    # shortcut: the form's save (execute) is for Experio administrators; creating the form already wrote the
    # dossier through its related fields, so what the dossier holds now says whether it went through.
    odoo_call("res.config.settings", "create", values, context=context)
    held = odoo_call("res.company", "read", [company_id], fields=list(values), context=context)[0]
    return all((held[k][0] if isinstance(held[k], list) else held[k]) == v for k, v in values.items())


@step_tool
def configure_cabinet(invoice_id: int) -> dict:
    """Fill the dossier's settings the invoice needs to be posted, as Experio's own screens do:
    the taxes its TVA accounts call for, then Configuration's "Detection automatique", which fills
    every empty setting from the chart of accounts and the journals. What still blocks after that stops the
    invoice: a setting the dossier lacks, for the cabinet, apart from what Experio refuses on the invoice itself."""
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    if invoice["state"] != "draft":
        return routine(f"Facture {invoice_id} : en état {invoice['state']}, rien à paramétrer.")
    company_id, context = invoice["company_id"][0], invoice_context(invoice)
    with refused(f"configure_cabinet {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier la facture."):
        before = blocking(invoice_id, context)
    if not before:
        return routine(f"Facture {invoice_id} : le dossier a les parametres necessaires.")
    if dry_run():
        return ok(
            f"{TEST_MODE}Facture {invoice_id} : Experio signale {' ; '.join(before)} La détection automatique des "
            "paramètres du dossier serait lancée."
        )
    with refused(f"configure_cabinet {invoice_id}", f"Facture {invoice_id} : Experio a refusé le paramétrage."):
        taxes = tax_count(company_id)
        tva_accounts = odoo_call("exp.account", "search", [("company_id", "=", company_id), *TVA_ACCOUNTS])
        if tva_accounts:  # the taxes the chart calls for, as Experio makes them from its own accounts
            odoo_json("exp.account", "create_tax_ids", tva_accounts, context=context)
        odoo_json("res.company", "auto_detect_settings", [company_id], context=context)  # Configuration's button
        made = tax_count(company_id) - taxes
        company = odoo_call("res.company", "read", [company_id], fields=amount_fields(), context=context)[0]
        values, names, cabinet = settings_needed(invoice, company)
        if values and not save_settings(company_id, values, context):
            cabinet, names = [*names, *cabinet], []
        after = checked(invoice_id, context)
    done = f"Facture {invoice_id} : détection automatique des paramètres du dossier lancée"
    if made:
        done += f", {made} taxe(s) créée(s)"
    if names:
        done += f", {', '.join(names)} active(s) dans le dossier"
    settings = [text for level, text in after if level == "cgnc"]
    settings += [f"{name} : compte à choisir" for name in cabinet]
    refused_here = [text for level, text in after if level != "cgnc"]
    if not (settings or refused_here):
        return ok(f"{done} : plus rien ne bloque la comptabilisation.")
    told = []
    if settings:
        told.append("À régler par le cabinet dans la Configuration du dossier : " + listed(settings))
    if refused_here:
        told.append("Sur la facture elle-même, Experio signale : " + listed(refused_here))
    return stop(f"{done}. Pas comptabilisable en l'état. " + " ".join(told))
