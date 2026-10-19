"""The dossier's settings posting needs, filled as Experio's own screens fill them.

Before posting, Experio says which setting is missing (collect_validation_messages: the journal and the
tiers account of local or foreign purchases and sales, the stamp, debours, BTP and withholding accounts, the
tax of the invoice's rate...). Its Configuration screen has a "Detection automatique" button
(res.company.auto_detect_settings) that fills every unset setting from the chart of accounts and the journals,
by their codes; and its chart of accounts makes the taxes its TVA accounts call for (exp.account.create_tax_ids:
one tax per rate and type from the 3455 and 4455 accounts, the 0 % ones always). This step runs those two and
reads again what still blocks: an account or a journal the dossier lacks is the cabinet's to create.
"""

from tarik.core.app import ok, routine, step_tool
from tarik.core.config import TEST_MODE
from tarik.core.experio import dry_run, invoice_context, odoo_call, odoo_json, plain, refused, require_invoice

TVA_ACCOUNTS = ["|", ("code", "=like", "3455%"), ("code", "=like", "4455%")]


def blocking(invoice_id, context):
    """What Experio's own check says is missing or wrong before posting ([str], plain text), warnings aside."""
    messages = odoo_json("exp.invoice", "collect_validation_messages", [invoice_id], context=context) or []
    return [plain(text) for level, text in messages if level != "warning"]


def tax_count(company_id):
    return odoo_call("exp.tax", "search_count", [("company_id", "=", company_id)])


@step_tool
def configure_cabinet(invoice_id: int) -> dict:
    """Fill the dossier's settings the invoice needs to be posted, as Experio's own screens do:
    the taxes its TVA accounts call for, then Configuration's "Detection automatique", which fills
    every empty setting from the chart of accounts and the journals. Says what still blocks after
    that: an account or a journal the dossier lacks, for the cabinet."""
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id"])
    if invoice["state"] != "draft":
        return routine(f"Facture {invoice_id} : en etat {invoice['state']}, rien a parametrer.")
    company_id, context = invoice["company_id"][0], invoice_context(invoice)
    with refused(f"configure_cabinet {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu verifier la facture."):
        before = blocking(invoice_id, context)
    if not before:
        return routine(f"Facture {invoice_id} : le dossier a les parametres necessaires.")
    if dry_run():
        return ok(
            f"{TEST_MODE}Facture {invoice_id} : Experio signale {' ; '.join(before)} La detection automatique des "
            "parametres du dossier serait lancee."
        )
    with refused(f"configure_cabinet {invoice_id}", f"Facture {invoice_id} : Experio a refuse le parametrage."):
        taxes = tax_count(company_id)
        tva_accounts = odoo_call("exp.account", "search", [("company_id", "=", company_id), *TVA_ACCOUNTS])
        if tva_accounts:  # the taxes the chart calls for, as Experio makes them from its own accounts
            odoo_json("exp.account", "create_tax_ids", tva_accounts, context=context)
        odoo_json("res.company", "auto_detect_settings", [company_id], context=context)  # Configuration's button
        made = tax_count(company_id) - taxes
        after = blocking(invoice_id, context)
    done = f"Facture {invoice_id} : detection automatique des parametres du dossier lancee"
    done += f", {made} taxe(s) creee(s)" if made else ""
    if after:
        return ok(f"{done}. Reste a parametrer par le cabinet : " + " ; ".join(after))
    return ok(f"{done} : plus rien ne bloque la comptabilisation.")
