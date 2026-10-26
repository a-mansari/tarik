"""The dossier's taxes for the invoice's TVA rates, made as the cabinet's training team makes them by hand.

When a rate of the invoice has no tax in the dossier, Experio's refresh says "La taxe avec le taux 10 n'existe pas
ou n'est pas configuree". The cabinet then creates it (Configuration > Comptabilite > Taxes):
- its name, "TVA <taux>% ACHATS|VENTES", with " (Immo)" for an immobilisation's;
- its side and its rate;
- its TVA account: 3455 for a purchase (34552 on charges), 34551 for an immobilisation (is_immo), 4455 for a sale;
- its DGI code, from the cabinet's table;
- the prefix and length Experio detects that account by.
Then it refreshes the invoice. Experio resets the DGI code on each line of each invoice, so one tax per rate, side
and account is enough. The tax is also listed in the dossier's settings (Configuration > General > Achats >
Autres taxes, or Ventes), where Experio looks for it.
"""

from collections import Counter

from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import account_code, dry_run, odoo_call, odoo_json

TVA_ACCOUNTS = ["|", ("code", "=like", "3455%"), ("code", "=like", "4455%")]  # what Experio makes its taxes from

RATES = (3, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20)  # exp.invoice's amount_tva_<rate> fields
# The cabinet's table: (side, rate, imported: the invoice has an import folder) -> DGI code. A purchase of services
# (6124, 6134, 6141) is code 140 at 20 %, a sale of services (7124, 7125) code 81. Any other: no code.
DGI_CODES = {
    ("purchase", 20, False): 146,
    ("purchase", 20, True): 145,
    ("purchase", 10, False): 150,
    ("purchase", 10, True): 149,
    ("sale", 20, False): 80,
}
SERVICES = {("purchase", 20): (("6124", "6134", "6141"), 140), ("sale", 20): (("7124", "7125"), 81)}
INVOICE_FIELDS = [
    "type",
    "company_id",
    "account_id",
    "folder_id",
    "is_local_invoice",
    "amount_tax_non_recoverable",
    "amount_untaxed",
    "amount_tva",
    *(f"amount_tva_{rate}" for rate in RATES),
]
GAP = 1.0  # MAD: how far the rates' amounts may be from the invoice's TVA (roundings)


def rates_used(invoice):
    """The TVA rates the invoice really has: its amounts per rate when they add up to its TVA, else the one rate its
    TVA and HT give (none when none fits). A stray amount Experio read under a rate (3.22 « TVA 3 % » next to 260
    at 20 % on a 260 TVA) never makes a tax."""
    rates = [rate for rate in RATES if invoice[f"amount_tva_{rate}"]]
    if abs(sum(invoice[f"amount_tva_{rate}"] for rate in rates) - (invoice["amount_tva"] or 0)) <= GAP:
        return rates
    untaxed, tva = invoice["amount_untaxed"] or 0, invoice["amount_tva"] or 0
    implied = [rate for rate in RATES if untaxed and abs(untaxed * rate / 100 - tva) <= GAP]
    return implied[:1]


def needs_zero_rate(invoice):
    """True when Experio books the invoice on its side's 0 % tax (generate_tva_line_values asks get_tax_id for rate
    0): TVA non récupérable / non facturée, or no TVA at all on an untaxed amount; never a foreign purchase."""
    if invoice["type"] == "purchase" and not invoice["is_local_invoice"]:
        return False
    no_tva = not invoice["amount_tva"] and (invoice["amount_untaxed"] or 0) > 0
    return bool(invoice["amount_tax_non_recoverable"] or no_tva)


def zero_rate_tax(company_id, side, context):
    """The dossier's 0 % tax of ``side``: Experio's own (create_tax_ids always makes "TVA 0% ACHATS" and "TVA 0%
    VENTES", its DGI code its own), made by it when missing. Its id, None when the dossier has no TVA account to make
    it from; True in test mode when Experio would make it."""
    domain = [("company_id", "=", company_id), ("type", "=", side), ("rate", "=", 0), ("is_immo_tax", "=", False)]
    found = odoo_call("exp.tax", "search", domain, limit=1)
    if found:
        return found[0]
    accounts = odoo_call("exp.account", "search", [("company_id", "=", company_id), *TVA_ACCOUNTS])
    if not accounts:
        return None
    if dry_run():
        return True
    odoo_json("exp.account", "create_tax_ids", accounts, context=context)
    found = odoo_call("exp.tax", "search", domain, limit=1)
    return found[0] if found else None


def settings_field(side, immo):
    """The dossier's setting listing the taxes Experio picks from for this side."""
    if side == "sale":
        return "sale_tax_ids"
    return "purchase_immo_tax_ids" if immo else "purchase_exploit_tax_ids"


def tva_account(company_id, side, immo, taxes):
    """The TVA account ({id, code}) a new tax of the dossier takes: the one its other taxes of that kind use, else
    by its code (34551 for an immobilisation, 34552 then 3455 for a purchase's charges, 4455 for a sale). None."""
    used = Counter(tuple(tax["account_id"]) for tax in taxes if tax["account_id"])
    if used:
        account_id, name = used.most_common(1)[0][0]
        return {"id": account_id, "code": name.split(" ")[0]}
    if side == "sale":
        prefixes = ("4455",)
    elif immo:
        prefixes = ("34551",)
    else:
        prefixes = ("34552", "3455")
    for prefix in prefixes:
        domain = [("company_id", "=", company_id), ("code", "=like", f"{prefix}%")]
        if prefix == "3455":
            domain.append(("code", "not like", "34551%"))  # an immobilisation's, never a charge's
        found = odoo_call("exp.account", "search_read", domain, fields=["code"], order="code", limit=1)
        if found:
            return found[0]
    return None


def dgi_code(side, rate, imported, account):
    """The id of the tax's DGI code (exp.tax.code) from the cabinet's table, None when the table has none."""
    prefixes, services = SERVICES.get((side, rate), ((), None))
    code = services if account.startswith(prefixes) and prefixes else DGI_CODES.get((side, rate, imported))
    if not code:
        return None
    found = odoo_call("exp.tax.code", "search", [("code", "=", code), ("type", "=", side), ("rate", "=", rate)])
    return found[0] if found else None


def tax_values(invoice, rate, account, immo):
    """The new tax as the cabinet fills its form."""
    side = invoice["type"]
    name = f"TVA {rate}% {'VENTES' if side == 'sale' else 'ACHATS'}" + (" (Immo)" if immo else "")
    return {
        "name": name,
        "type": side,
        "is_immo_tax": account["code"].startswith("34551"),
        "rate": rate,
        "account_id": account["id"],
        "code_id": dgi_code(side, rate, bool(invoice["folder_id"]), account_code(invoice)) or False,
        "account_prefix": account["code"][:5],
        "account_length": len(account["code"]),
        "company_id": invoice["company_id"][0],
    }


def listed(company_id, field, tax_id, context):
    """The tax listed in the dossier's setting ``field``, as its Configuration screen lists it (the form's creation
    writes the dossier, see configuration.save_settings): "listed" already, "added", or "refused" (the cabinet's
    rights)."""
    held = odoo_call("res.company", "read", [company_id], fields=[field], context=context)[0][field]
    if tax_id in held:
        return "listed"
    odoo_call("res.config.settings", "create", {field: [(6, 0, [*held, tax_id])]}, context=context)
    held = odoo_call("res.company", "read", [company_id], fields=[field], context=context)[0][field]
    return "added" if tax_id in held else "refused"


def add_missing_taxes(invoice_id, context):
    """For each TVA rate of the invoice, the dossier's tax of that rate, side and kind (immobilisation or not):
    created when missing, listed in the dossier's settings when not. What was done or is left to the cabinet
    ([str]); [] when every rate has its tax."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=INVOICE_FIELDS, context=context)[0]
    side, company_id = invoice["type"], invoice["company_id"][0]
    immo = side == "purchase" and account_code(invoice).startswith("2")  # an immobilisation's account, class 2
    field = settings_field(side, immo)
    domain = [("company_id", "=", company_id), ("type", "=", side), ("is_immo_tax", "=", immo)]
    taxes = odoo_call("exp.tax", "search_read", domain, fields=["rate", "account_id"])
    notes = []
    if needs_zero_rate(invoice) and not immo:
        tax_id = zero_rate_tax(company_id, side, context)
        if not tax_id:
            notes.append("taxe 0 % : aucun compte de TVA dans le dossier pour la créer, à créer par le cabinet")
        elif tax_id is True:
            notes.append(f"{TEST_MODE}taxe 0 % : Experio la créerait depuis les comptes de TVA du dossier")
        elif not dry_run():
            state = listed(company_id, field, tax_id, context)
            if state == "added":
                notes.append("taxe 0 % ajoutée aux taxes du dossier (Configuration > Général)")
            elif state == "refused":
                notes.append("taxe 0 % à ajouter par le cabinet dans Configuration > Général (autres taxes)")
    for rate in rates_used(invoice):
        found = [tax["id"] for tax in taxes if tax["rate"] == rate]
        if not found:
            account = tva_account(company_id, side, immo, taxes)
            if not account:
                notes.append(f"taxe {rate} % : aucun compte de TVA dans le dossier, à créer par le cabinet")
                continue
            values = tax_values(invoice, rate, account, immo)
            if dry_run():
                notes.append(f"taxe `{values['name']}` serait créée (compte `{account['code']}`)")
                continue
            found = [odoo_call("exp.tax", "create", values, context=context)]
            notes.append(f"taxe `{values['name']}` créée (compte `{account['code']}`)")
        if dry_run():
            continue
        state = listed(company_id, field, found[0], context)
        if state == "added":
            notes.append(f"taxe {rate} % ajoutee aux taxes du dossier (Configuration > General)")
        elif state == "refused":
            notes.append(f"taxe {rate} % a ajouter par le cabinet dans Configuration > General (autres taxes)")
    return notes
