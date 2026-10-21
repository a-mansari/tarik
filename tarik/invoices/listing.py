"""The dossier's invoices, as Experio lists them, filtered by where they stand."""

from tarik.core.app import ToolError, ok, tool
from tarik.core.experio import odoo_call, refused, require_dossier
from tarik.core.words import without_if

# The statuses the agent may filter by -> Experio's own domain. Exclusive: each invoice is in one of them.
STATUSES = {
    "en cours": [("state", "=", "pending")],  # Experio is still reading it
    "brouillon": [("state", "=", "draft")],
    "comptabilisee": [("state", "=", "post"), ("payment_state", "!=", "paid")],
    "payee": [("state", "=", "post"), ("payment_state", "=", "paid")],
    "exportee": [("state", "=", "export")],  # paid or not, as Experio's state shows it
}
# posted: by its payment; cancelled ones are never listed
LABELS = {"pending": "en cours", "draft": "brouillon", "export": "exportee"}
SIDES = {"achat": ("purchase", "d'achat"), "vente": ("sale", "de vente")}  # the side -> Experio's type, its words


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


@tool
def list_invoices(dossier_id: int, status: str = "", limit: int = 20, side: str = "") -> dict:
    """List a dossier's invoices, most recent first. ``status`` filters them, exactly one of: "en cours" (Experio
    still reading), "brouillon", "comptabilisee" (posted, not paid: the unpaid ones), "payee", "exportee"; empty for
    all of them. ``side``: "achat", "vente", or empty for both. The agent chooses them from what the client asked;
    any other value is refused."""
    dossier = require_dossier(dossier_id)
    wanted = (status or "").strip()
    if wanted and wanted not in STATUSES:
        raise ToolError(f"Statut « {status} » inconnu : {', '.join(STATUSES)}, ou vide pour tout.")
    side = (side or "").strip()
    if side and side not in SIDES:
        raise ToolError(f"Type « {side} » inconnu : achat, vente, ou vide pour les deux.")
    domain = [("company_id", "=", dossier_id), ("state", "!=", "cancel"), *STATUSES.get(wanted, [])]
    if side:
        domain.append(("type", "=", SIDES[side][0]))
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
    what = " ".join(word for word in ("facture(s)", SIDES.get(side, ("", ""))[1], wanted) if word)
    if not found:
        return ok(f"Aucune {what.replace('(s)', '')} dans le dossier {dossier}.")
    lines = [line(invoice) for invoice in found]
    more = f" (les {len(found)} plus recentes)" if total > len(found) else ""
    return ok(f"{total} {what} dans le dossier {dossier}{more} :\n" + "\n".join(lines))
