"""The dossier's invoices, as Experio lists them, filtered by where they stand."""

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server
from tarik.core.config import POSTED
from tarik.core.experio import odoo_call, refused, require_dossier
from tarik.core.text import without_if

# The statuses the agent may filter by -> Experio's own domain. Exclusive: each invoice is in one of them.
STATUSES = {
    "en cours": [("state", "=", "pending")],  # Experio is still reading it
    "brouillon": [("state", "=", "draft")],
    "comptabilisee": [("state", "in", list(POSTED)), ("payment_state", "!=", "paid")],
    "payee": [("state", "in", list(POSTED)), ("payment_state", "=", "paid")],
}
LABELS = {"pending": "en cours", "draft": "brouillon"}  # posted: by its payment; cancelled ones are never listed


def label(invoice):
    """Where ``invoice`` (with state and payment_state) stands, as the client reads it."""
    if invoice["state"] in LABELS:
        return LABELS[invoice["state"]]
    return "payee" if invoice["payment_state"] == "paid" else "comptabilisee"


def amount(value):
    """An amount as Experio shows it: two decimals, thousands spaced (999.9960000000001 is 1 000.00)."""
    return f"{float(value or 0):,.2f}".replace(",", " ")


def line(invoice):
    """One invoice of the list: "- facture 12 : F-001, ATLAS, 2026-03-15, 1 200.00 DH, brouillon"."""
    tiers = without_if((invoice["partner_id"] or [0, "tiers inconnu"])[1])
    return (
        f"- facture {invoice['id']} : {invoice['invoice_number'] or 'sans numero'}, {tiers}, "
        f"{invoice['invoice_date'] or 'sans date'}, {amount(invoice['amount_total'])} DH, {label(invoice)}"
    )


@server.tool()
def list_invoices(dossier_id: int, status: str = "", limit: int = 20) -> dict:
    """List a dossier's invoices, most recent first. ``status`` filters them, exactly one of: "en cours" (Experio
    still reading), "brouillon", "comptabilisee" (posted, not paid: the unpaid ones), "payee"; empty for all of
    them. The agent chooses it from what the client asked; any other value is refused."""
    dossier = require_dossier(dossier_id)
    wanted = (status or "").strip()
    if wanted and wanted not in STATUSES:
        raise ToolError(f"Statut « {status} » inconnu : en cours, brouillon, comptabilisee, payee, ou vide pour tout.")
    domain = [("company_id", "=", dossier_id), ("state", "!=", "cancel"), *STATUSES.get(wanted, [])]
    limit = max(1, min(int(limit or 20), 100))
    with refused(f"list_invoices {dossier_id}", "Experio n'a pas pu donner la liste des factures."):
        total = odoo_call("exp.invoice", "search_count", domain)
        found = odoo_call(
            "exp.invoice",
            "search_read",
            domain,
            order="id desc",
            limit=limit,
            fields=["invoice_number", "invoice_date", "partner_id", "amount_total", "state", "payment_state"],
        )
    what = f"facture(s) {wanted}" if wanted else "facture(s)"
    if not found:
        return ok(f"Aucune {what.replace('(s)', '')} dans le dossier {dossier}.")
    lines = [line(invoice) for invoice in found]
    more = f" (les {len(found)} plus recentes)" if total > len(found) else ""
    return ok(f"{total} {what} dans le dossier {dossier}{more} :\n" + "\n".join(lines))
