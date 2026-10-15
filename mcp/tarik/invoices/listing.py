"""The dossier's invoices, as Experio lists them, filtered by where they stand."""

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server
from tarik.dossiers.search import normalized
from tarik.core.experio import odoo_call, require_dossier

# What the client asks for -> Experio's own domain. Exclusive: each invoice is in one of them.
STATUSES = {
    "en cours": [("state", "=", "pending")],  # Experio is still reading it
    "brouillon": [("state", "=", "draft")],
    "comptabilisee": [("state", "in", ["post", "export"]), ("payment_state", "!=", "paid")],
    "payee": [("state", "in", ["post", "export"]), ("payment_state", "=", "paid")],
}
WORDS = {"cours": "en cours", "lecture": "en cours", "pending": "en cours", "brouillon": "brouillon", "draft": "brouillon",
         "comptabilis": "comptabilisee", "valide": "comptabilisee", "post": "comptabilisee", "paye": "payee",
         "regle": "payee", "paid": "payee"}


def status_of(asked):
    """The status the client means ("Comptabilisées" -> "comptabilisee"), "" for all, None when unclear."""
    text = normalized(asked or "")
    if not text or text in ("tout", "toutes", "all"):
        return ""
    found = {status for word, status in WORDS.items() if word in text}
    return found.pop() if len(found) == 1 else None


def label(invoice):
    if invoice["state"] == "pending":
        return "en cours"
    if invoice["state"] == "draft":
        return "brouillon"
    if invoice["state"] == "cancel":
        return "abandonnee"
    return "payee" if invoice["payment_state"] == "paid" else "comptabilisee"


@server.tool()
def list_invoices(dossier_id: int, status: str = "", limit: int = 20) -> dict:
    """List a dossier's invoices, most recent first. ``status`` filters them:
    "en cours" (Experio still reading), "brouillon", "comptabilisee" (posted, not
    paid) or "payee"; empty for all of them."""
    dossier = require_dossier(dossier_id)
    wanted = status_of(status)
    if wanted is None:
        raise ToolError(f"Statut « {status} » inconnu : en cours, brouillon, comptabilisee ou payee.")
    domain = [("company_id", "=", dossier_id), ("state", "!=", "cancel"), *STATUSES.get(wanted, [])]
    limit = max(1, min(int(limit or 20), 100))
    try:
        total = odoo_call("exp.invoice", "search_count", domain)
        found = odoo_call("exp.invoice", "search_read", domain, order="id desc", limit=limit,
                          fields=["invoice_number", "invoice_date", "partner_id", "amount_total", "state", "payment_state"])
    except Exception as error:
        print(f"list_invoices {dossier_id}: {error}", flush=True)
        raise ToolError("Experio n'a pas pu donner la liste des factures.") from None
    what = f"facture(s) {wanted}" if wanted else "facture(s)"
    if not found:
        return ok(f"Aucune {what.replace('(s)', '')} dans le dossier {dossier}.")
    lines = [f"- facture {i['id']} : {i['invoice_number'] or 'sans numero'}, "
             f"{(i['partner_id'] or [0, 'tiers inconnu'])[1].split(' - ')[0]}, {i['invoice_date'] or 'sans date'}, "
             f"{i['amount_total']} DH, {label(i)}" for i in found]
    more = f" (les {len(found)} plus recentes)" if total > len(found) else ""
    return ok(f"{total} {what} dans le dossier {dossier}{more} :\n" + "\n".join(lines))
