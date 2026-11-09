"""What Tarik may read of a dossier, as data: the models and fields the agent's queries (query.py) may reach, nothing
else. The agent never sees Odoo's own schema: a model or a field missing here cannot be asked for, filtered on,
grouped, summed or sorted by, so nothing sensitive is readable by default (no user, password, bank account, payroll
per person, attachment or setting is listed). Each model is read inside one dossier (``company_id`` is forced by the
query, never asked), with its ``base`` terms always added.

Experio's own definitions each entry stands on (../experio_app, addons/):
- exp.invoice: state, addons/invoice/models/exp_invoice.py:2199; payment_state, :2263; type, :1999; the soft-delete
  flag exp_deleted, :13120.
- exp.move.line: parent_state is the entry's state (related, stored), exp_account/models/exp_move.py:3345; exp_deleted
  (deleted lines are out of the totals), :3350. The base terms are the FEC's own (dossiers/export.py fec_lines).
- exp.journal: journal_category, exp_account/models/exp_journal.py:48 (normal, a_nouveau, cloture).
- exp.fiscal.entries: state, exp_account/models/exp_fiscal_entries.py:405.
- exp.bank.statement.line: state, bank/models/bank_statement_line.py:430."""

from dataclasses import dataclass, field
from typing import Final

# A field's kind decides the operators a filter may use, and whether it may be grouped on or summed.
ID, LINK, TEXT, NUMBER, DATE, CHOICE, FLAG = "id", "link", "text", "number", "date", "choice", "flag"

OPERATORS: Final = {
    ID: ("=", "in"),
    LINK: ("=", "!=", "in"),
    TEXT: ("=", "ilike", "in"),
    NUMBER: ("=", "!=", "<", "<=", ">", ">="),
    DATE: ("=", "<", "<=", ">", ">="),
    CHOICE: ("=", "!=", "in"),
    FLAG: ("=",),
}
GROUPABLE: Final = (LINK, DATE, CHOICE, FLAG)
SORTABLE: Final = (ID, TEXT, NUMBER, DATE, CHOICE, FLAG)  # a link sorts by another model's name: not offered


@dataclass(frozen=True)
class Column:
    """One readable field: its kind, what it means (French, as the agent reads it) and, for a choice, its values."""

    kind: str
    label: str
    values: dict = field(default_factory=dict)  # choice: {value: meaning}


@dataclass(frozen=True)
class Source:
    """One readable model: its columns, the terms every read adds, and its default order."""

    label: str
    columns: dict
    base: tuple = ()
    order: str = "id desc"


def c(kind, label, **values):
    return Column(kind, label, values)


MODELS: Final = {
    "exp.invoice": Source(
        "Factures d'achat et de vente (brouillons compris, supprimées exclues)",
        {
            "id": c(ID, "numéro de la facture"),
            "invoice_number": c(TEXT, "numéro de facture du document"),
            "invoice_date": c(DATE, "date de facture"),
            "invoice_date_due": c(DATE, "date d'échéance"),
            "type": c(CHOICE, "sens", purchase="achat", sale="vente"),
            "refund": c(FLAG, "avoir (vrai) ou facture"),
            "state": c(
                CHOICE,
                "statut dans Experio",
                cancel="abandonnée",
                pending="en cours de lecture",
                draft="brouillon",
                post="comptabilisée",
                export="exportée",
            ),  # fmt: skip
            "payment_state": c(CHOICE, "paiement", unpaid="impayée", partial="payée en partie", paid="payée"),
            "partner_id": c(LINK, "tiers (fournisseur ou client)"),
            "partner_name": c(TEXT, "nom du tiers lu sur le document"),
            "partner_ice": c(TEXT, "ICE du tiers"),
            "journal_id": c(LINK, "journal"),
            "amount_untaxed": c(NUMBER, "montant HT"),
            "amount_tva": c(NUMBER, "montant de TVA"),
            "amount_total": c(NUMBER, "montant TTC"),
            "amount_residual": c(NUMBER, "reste dû TTC"),
        },
        base=(("exp_deleted", "=", False),),
        order="invoice_date desc, id desc",
    ),
    "exp.move.line": Source(
        "Lignes d'écritures comptables comptabilisées ou exportées (grand livre ; débit et crédit en MAD)",
        {
            "id": c(ID, "numéro de la ligne"),
            "date": c(DATE, "date de l'écriture"),
            "move_id": c(LINK, "écriture"),
            "journal_id": c(LINK, "journal"),
            "account_id": c(LINK, "compte (code et libellé)"),
            "partner_id": c(LINK, "tiers"),
            "name": c(TEXT, "libellé"),
            "debit": c(NUMBER, "débit"),
            "credit": c(NUMBER, "crédit"),
            "reconciled": c(FLAG, "lettrée (rapprochée)"),
        },
        base=(
            ("parent_state", "in", ["posted", "exported"]),
            ("exp_deleted", "=", False),
            ("move_id.exp_deleted", "=", False),
        ),
        order="date, id",
    ),
    "exp.account": Source(
        "Plan comptable du dossier",
        {
            "id": c(ID, "numéro du compte"),
            "code": c(TEXT, "code du compte"),
            "name": c(TEXT, "libellé du compte"),
            "reconcile": c(FLAG, "compte de tiers lettrable"),
        },
        order="code",
    ),
    "exp.journal": Source(
        "Journaux du dossier",
        {
            "id": c(ID, "numéro du journal"),
            "code": c(TEXT, "code du journal"),
            "name": c(TEXT, "nom du journal"),
            "type": c(
                CHOICE,
                "type",
                sale="vente",
                purchase="achat",
                cash="caisse",
                bank="banque",
                general="opérations diverses",
            ),
            "journal_category": c(CHOICE, "catégorie", normal="normal", a_nouveau="à-nouveaux", cloture="clôture"),
        },
        order="name",
    ),
    "exp.partner": Source(
        "Tiers du dossier (clients et fournisseurs)",
        {
            "id": c(ID, "numéro du tiers"),
            "name": c(TEXT, "nom"),
            "ice": c(TEXT, "ICE"),
            "vat": c(TEXT, "identifiant fiscal (IF)"),
            "third_party_supplier_account": c(TEXT, "compte fournisseur"),
            "third_party_customer_account": c(TEXT, "compte client"),
        },
        order="name",
    ),
    "exp.fiscal.entries": Source(
        "Exercices comptables du dossier",
        {
            "id": c(ID, "numéro de l'exercice"),
            "name": c(TEXT, "nom"),
            "date_start": c(DATE, "début"),
            "date_end": c(DATE, "fin"),
            "state": c(CHOICE, "statut", draft="brouillon", open="ouvert", close="clôturé", shut="clôture en cours"),
        },
        order="date_end desc",
    ),
    "exp.bank.statement.line": Source(
        "Opérations des relevés bancaires (supprimées exclues)",
        {
            "id": c(ID, "numéro de l'opération"),
            "date": c(DATE, "date"),
            "payment_ref": c(TEXT, "libellé"),
            "amount": c(NUMBER, "montant (négatif : sortie)"),
            "debit": c(NUMBER, "débit"),
            "credit": c(NUMBER, "crédit"),
            "state": c(
                CHOICE,
                "statut dans Experio",
                pending="en attente",
                draft="brouillon",
                ready="prête",
                suspense="en suspens",
                post="comptabilisée",
                export="exportée",
            ),  # fmt: skip
            "journal_id": c(LINK, "journal bancaire"),
            "partner_id": c(LINK, "tiers"),
        },
        base=(("exp_deleted", "=", False),),
        order="date desc, id desc",
    ),
}
