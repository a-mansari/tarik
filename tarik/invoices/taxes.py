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

from tarik.core.experio import account_code, dry_run, odoo_call

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
INVOICE_FIELDS = ["type", "company_id", "account_id", "folder_id", *(f"amount_tva_{rate}" for rate in RATES)]


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
    prefixes = ("4455",) if side == "sale" else ("34551",) if immo else ("34552", "3455")
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
    for rate in [rate for rate in RATES if invoice[f"amount_tva_{rate}"]]:
        found = [tax["id"] for tax in taxes if tax["rate"] == rate]
        if not found:
            account = tva_account(company_id, side, immo, taxes)
            if not account:
                notes.append(f"taxe {rate} % : aucun compte de TVA dans le dossier, a creer par le cabinet")
                continue
            values = tax_values(invoice, rate, account, immo)
            if dry_run():
                notes.append(f"taxe {values['name']} serait creee (compte {account['code']})")
                continue
            found = [odoo_call("exp.tax", "create", values, context=context)]
            notes.append(f"taxe {values['name']} creee (compte {account['code']})")
        if dry_run():
            continue
        state = listed(company_id, field, found[0], context)
        if state == "added":
            notes.append(f"taxe {rate} % ajoutee aux taxes du dossier (Configuration > General)")
        elif state == "refused":
            notes.append(f"taxe {rate} % a ajouter par le cabinet dans Configuration > General (autres taxes)")
    return notes
