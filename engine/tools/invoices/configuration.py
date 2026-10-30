"""Fill the dossier settings an invoice needs to be posted, the way Experio's own screens fill them.

Before posting, Experio lists what is missing (exp.invoice collect_validation_messages: the journal and the tiers
account of local or foreign purchases and sales, the stamp, debours, BTP and withholding accounts, the tax of the
invoice's rate...). Experio offers two fixes, which this step runs:
- the Configuration screen's « Détection automatique » button (res.company auto_detect_settings): fills each unset
  setting with its default, found by code in the chart of accounts and the journals;
- exp.account create_tax_ids: makes the taxes the TVA accounts call for, one per rate and type from the 3455 and
  4455 accounts, plus the 0 % purchase and sale taxes when missing.
Then it turns on the switches the detection leaves off (a switch is never auto-detected) for each amount the
invoice carries (retenue de garantie, prorata, assurance, avance, timbre, debours, taxes de séjour), which Experio
would otherwise drop silently, with the amount's account found by its CGNC code when the detection left it empty.
Finally it reads again what still blocks, kept apart: a setting the dossier lacks (Experio's "cgnc" level, or an
account only the cabinet can choose) is for the cabinet in Configuration; what Experio refuses on the invoice
itself (an amount, a field) is no setting. Either stops the invoice before it is reported verified. Experio's
warnings block nothing here: settle_warnings handles each.
"""

from engine.tools.core.app import ok, routine, step_tool, stop
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    blocking,
    checked,
    dry_run,
    invoice_context,
    odoo_call,
    odoo_json,
    refused,
    require_invoice,
)
from engine.tools.core.words import counted

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
    "amount_tsc": (
        "taxe de services communaux",
        ("use_tsc_purchase_tax", "tsc_account_id", None),
        ("use_tsc_sale_tax", "tsc_sale_account_id", None),
    ),
    # A purchase's taxe régionale is booked on the invoice's own account when the dossier has none
    # (_get_tr_style_config): only its switch is needed. A sale's needs the dossier's account, which Experio checks
    # at posting (validate_activity_type_settings: missing_regional_tax_account).
    "amount_tva_3": ("taxe régionale", ("use_amount_tva_3", None, None), (None, None, None)),
    "amount_regional_tax": (
        "taxe régionale",
        (None, None, None),
        ("use_regional_tax", "regional_tax_account_id", None),
    ),
}
FOREIGN_IGNORED = {"amount_guarantee_holdback", "amount_prorata", "amount_insurance", "advance_amount"}
INVOICE_FIELDS = ["state", "company_id", "exercice_id", "type", "is_local_invoice", *AMOUNTS]


ASSETS_MENU = "Configuration > Immobilisations > Catégories"


def assets_without_category(invoice_id, context):
    """Return the names of the invoice's class 2 accounts whose asset line has no asset category; [] when
    Experio's immobilisations module is not installed.

    Experio fills an asset line's category from the category set on its account (immobilisations
    _onchange_tva_line_ids_for_asset_generation, at each refresh); a line without one gets no asset at posting,
    silently (exp.invoice.asset.line generate_asset_assets)."""
    if not odoo_call("exp.invoice", "fields_get", ["invoice_asset_line_ids"], attributes=["type"]):
        return []
    lines = odoo_call("exp.invoice", "read", [invoice_id], fields=["invoice_asset_line_ids"], context=context)[0]
    if not lines["invoice_asset_line_ids"]:
        return []
    fields = ["account_id", "asset_category_id"]
    found = odoo_call("exp.invoice.asset.line", "read", lines["invoice_asset_line_ids"], fields=fields, context=context)
    return [line["account_id"][1] for line in found if line["account_id"] and not line["asset_category_id"]]


def assets_text(accounts):
    """Return the French note telling the cabinet to create an asset category for each of ``accounts`` (names)."""
    return (
        f"catégorie d'immobilisation à créer pour le compte {', '.join(f'`{a}`' for a in accounts)} ({ASSETS_MENU}), "
        "sans quoi Experio ne crée pas l'immobilisation à la comptabilisation"
    )


# Per folder number rule (the dossier's import_folder_number / export_folder_number), the invoice of the folder whose
# number becomes the folder's: Experio's own rule when it reads the folder's invoices (base_global exp_bluejay_token.py
# parser_folder_values), applied again when Tarik corrected that number after the reading.
NUMBERED_BY = {
    "transit_invoice": {"import": "transit", "export": "transit"},
    "commercial_invoice": {"import": "supplier", "export": "customer"},
}


def settle_folder_number(invoice_id, context):
    """Give the invoice's import (or export) folder its number when it has none, as Experio's reading does: the
    number of the folder's invoice that the dossier's rule names (NUMBERED_BY).

    Returns a phrase for the client naming the number set; "" when nothing was set (no folder, already numbered,
    no rule, no such invoice, or test mode)."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["folder_id"], context=context)[0]
    if not invoice["folder_id"]:
        return ""
    folder = odoo_call(
        "exp.import.folder", "read", [invoice["folder_id"][0]], fields=["folder_number", "folder_type", "company_id"]
    )[0]
    if folder["folder_number"]:
        return ""
    field = "export_folder_number" if folder["folder_type"] == "export" else "import_folder_number"
    rule = odoo_call("res.company", "read", [folder["company_id"][0]], fields=[field])[0][field]
    kind = NUMBERED_BY.get(rule, {}).get(folder["folder_type"])
    if not kind:
        return ""
    domain = [("folder_id", "=", folder["id"]), ("invoice_type", "=", kind), ("state", "!=", "cancel")]
    found = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number"], order="id", limit=1)
    if not found or not found[0]["invoice_number"] or dry_run():
        return ""
    odoo_call(
        "exp.import.folder", "write", [folder["id"]], {"folder_number": found[0]["invoice_number"]}, context=context
    )
    return f"numéro du dossier d'import : {found[0]['invoice_number']}"


def listed(texts):
    """Return ``texts`` joined as one sentence, "a ; b.", without doubling the period Experio's own texts end
    with."""
    return " ; ".join(text.rstrip(". ") for text in texts) + "."


def tax_count(company_id):
    return odoo_call("exp.tax", "search_count", [("company_id", "=", company_id)])


def account_by_code(company_id, prefixes):
    """Return the id of the dossier's account with the lowest code starting with ``prefixes[0]``, else with the next
    prefix, and so on; None when no prefix matches."""
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
    """Return the dossier's switch and account fields AMOUNTS names, sorted."""
    fields = set()
    for _, *sides in AMOUNTS.values():
        for switch, account, _ in sides:
            fields |= {field for field in (switch, account) if field}
    return sorted(fields)


def settings_needed(invoice, company):
    """Return what the invoice's amounts need the dossier to have and it lacks, as (values, names, cabinet).

    values: {res.company field: value} to write (switches turned on, accounts found by CGNC code).
    names: the amounts (French names) these values serve.
    cabinet: the amounts whose account no CGNC code finds: only the cabinet can choose it.
    Amounts Experio ignores on a foreign invoice are left out."""
    purchase, foreign = invoice["type"] == "purchase", not invoice["is_local_invoice"]
    values, names, cabinet = {}, [], []
    for amount, (name, supplier, customer) in AMOUNTS.items():
        switch, account, prefixes = supplier if purchase else customer
        ignored = foreign and ignored_abroad(amount, purchase)
        if not invoice[amount] or not (switch or account) or ignored:
            continue
        if account and not company[account]:
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
    """Write ``values`` ({res.company field: value}) as the Configuration screen does, through a res.config.settings
    form whose fields are related to the dossier's. True when the dossier now holds them all (the cabinet's rights
    decide)."""
    # shortcut: the form's save (execute) is for Experio administrators; creating the form already wrote the
    # dossier through its related fields, so what the dossier holds now says whether it went through.
    odoo_call("res.config.settings", "create", values, context=context)
    held = odoo_call("res.company", "read", [company_id], fields=list(values), context=context)[0]
    return all((held[k][0] if isinstance(held[k], list) else held[k]) == v for k, v in values.items())


@step_tool
def configure_cabinet(invoice_id: int) -> dict:
    """Fill the dossier settings a draft invoice needs to be posted, as Experio's own screens do: the taxes its TVA
    accounts call for, Configuration's « Détection automatique », then the switch and account of each amount
    the invoice carries. Also numbers the invoice's import folder when it has no number.

    Returns an ok or routine result when nothing blocks posting any more; else a stop result listing, apart, the
    settings the cabinet must set in Configuration and what Experio refuses on the invoice itself."""
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    if invoice["state"] != "draft":
        return routine(f"Facture {invoice_id} : en état {invoice['state']}, rien à paramétrer.")
    company_id, context = invoice["company_id"][0], invoice_context(invoice)
    with refused(f"configure_cabinet {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu vérifier la facture."):
        numbered = settle_folder_number(invoice_id, context)
        before = blocking(invoice_id, context)
        assets = assets_without_category(invoice_id, context)
    if not before and assets:
        return stop(
            f"Facture {invoice_id} : pas comptabilisable en l'état. À régler par le cabinet dans la Configuration du "
            f"dossier : {assets_text(assets)}."
        )
    if not before and numbered:
        return ok(f"Facture {invoice_id} : {numbered}.")
    if not before:
        return routine(f"Facture {invoice_id} : le dossier a les paramètres nécessaires.")
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
    done = f"Facture {invoice_id} : j'ai lancé la détection automatique des paramètres du dossier"
    if made:
        done += f". {counted(made, 'Elle a créé {} taxe', 'Elle a créé {} taxes')}"
    if names:
        done += f". J'ai activé dans le dossier : {', '.join(names)}"
    settings = [text for level, text in after if level == "cgnc"]
    settings += [f"{name} : compte à choisir" for name in cabinet]
    if assets_without_category(invoice_id, context):
        settings.append(assets_text(assets_without_category(invoice_id, context)))
    refused_here = [text for level, text in after if level == "error"]  # warnings: settle_warnings' own
    if not (settings or refused_here):
        return ok(f"{done}. Plus rien ne bloque la comptabilisation.")
    told = []
    if settings:
        told.append("À régler par le cabinet dans la Configuration du dossier : " + listed(settings))
    if refused_here:
        told.append("Sur la facture elle-même, Experio signale : " + listed(refused_here))
    return stop(f"{done}. La facture n'est pas comptabilisable en l'état. " + " ".join(told))
