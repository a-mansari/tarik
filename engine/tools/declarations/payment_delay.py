"""The payment delays declaration (déclaration des délais de paiement, loi 69-21), prepared as the cabinet does it on
Experio's own screen (module declaration, exp.payment.delay), declared only when the client asks and confirms.

- Whether: the dossier uses payment delays in Experio (res.company use_payment_delay); its mode, quarterly or yearly,
  is the dossier's (payment_delay_declaration_mode). Period: the one the client names, else the last ended.
- The turnover (CA) decides which mode the law requires (_get_required_declaration_mode); Experio never computes it,
  it is typed on the declaration (default: the dossier's setting). Asked when it is 0, as Experio itself asks.
- Lines: Experio's « Actualiser » (action_refresh: the period's overdue local purchases); it deletes the lines added
  by hand, so a declaration holding some is left as it is.
- Before declaring: Experio's own error (the mode the law requires for that CA), its refusals at « Déclarer » said
  first (period declared, tiers without IF), and the period still running (Experio doesn't check it); what it leaves
  out without a word (purchases of the period Experio doesn't follow for delays: supplier without valid RC, below the
  threshold, before the start date; those blocked for this declaration). The XML runs Experio's own checks.
- « Déclarer » (action_post) only on the client's words, after a confirmation; the DGI deposit stays a person's.
"""

import re
from datetime import date

from engine.tools.core.app import ok, question, step_tool, stop
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    ExperioRefusal,
    dry_run,
    experio_refusal,
    in_dossier,
    odoo_call,
    odoo_json,
    plain,
    refused,
    require_dossier,
)
from engine.tools.core.outbox import keep_record_file
from engine.tools.core.words import as_number, normalized
from engine.tools.declarations.tva import find_period, last_ended, period_named

TABLE = "exp.payment.delay"
LINE = "exp.payment.delay.line"
FIELDS = ["state", "period_id", "fiscal_year", "start_date", "end_date", "amount_turnover", "amount_penalty"]
FIELDS += ["error_msg", "bam_rate", "line_ids", "name"]
LINE_FIELDS = ["partner_id", "added_manually", "montant_facture_ttc", "montant_non_encore_paye"]
LINE_FIELDS += ["montant_paye_hors_delai", "amount_penalty"]


def period_of(mode, words, today):
    """(period number or None, year): the quarter named, else the last ended; the year named, else the year before,
    for a yearly dossier."""
    if mode == "yearly":
        year = re.search(r"(?<!\d)(20\d\d)(?!\d)", normalized(words or ""))
        return None, int(year.group(1)) if year else today.year - 1
    return period_named(mode, words, today) or last_ended(mode, today)


def declaration_of(dossier_id, period, year, context):
    """The dossier's declaration of the period: declared, else its draft, else a new one (None in test mode)."""
    domain = [("company_id", "=", dossier_id), ("fiscal_year", "=", str(year)), ("state", "!=", "cancel")]
    if period:
        domain.append(("period_id", "=", period["id"]))
    found = odoo_call(TABLE, "search_read", domain, fields=["state"], order="state desc, id desc", context=context)
    if found:
        return found[0]["id"], False
    if dry_run():
        return None, False
    values = {"company_id": dossier_id, "fiscal_year": str(year), "date_declaration": date.today().isoformat()}
    if period:
        values["period_id"] = period["id"]
    return odoo_call(TABLE, "create", values, context=context), True


def left_out(dossier_id, table, context):
    """The period's purchases Experio's computation leaves out without a word."""
    period = [("company_id", "=", dossier_id), ("type", "=", "purchase"), ("state", "in", ["post", "export"])]
    period += [("invoice_date", ">=", table["start_date"]), ("invoice_date", "<=", table["end_date"])]
    period += [("is_local_invoice", "=", True)]
    said = []
    untracked = odoo_call(
        "exp.invoice", "search_count", [*period, ("should_use_payment_delay", "=", False)], context=context
    )
    if untracked:
        said.append(
            f"{untracked} facture(s) d'achat de la période sans suivi de délai dans Experio (fournisseur sans "
            "RC valide, montant sous le seuil, ou avant la date de début)"
        )
    blocked = odoo_call("exp.invoice", "search_count", [*period, ("do_not_declare_ddp", "=", True)], context=context)
    if blocked:
        said.append(f"{blocked} facture(s) bloquée(s) pour cette déclaration dans Experio")
    return said


def totals(table, lines):
    if not lines:
        return "aucune facture en retard sur la période (déclaration à néant)."
    owed = sum(line["montant_non_encore_paye"] or 0 for line in lines)
    late = sum(line["montant_paye_hors_delai"] or 0 for line in lines)
    suppliers = len({line["partner_id"][0] for line in lines if line["partner_id"]})
    return (
        f"{len(lines)} ligne(s), {suppliers} fournisseur(s) ; "
        f"non encore payé {owed:.2f}, payé hors délai {late:.2f}, amende {table['amount_penalty'] or 0:.2f} "
        f"(taux BAM {table['bam_rate'] or 0:.2f} %)."
    )


@step_tool
def prepare_payment_delay_declaration(
    dossier_id: int,
    periode: str = "",
    chiffre_affaires: str = "",
    declarer: bool = False,
    client_confirmed: bool = False,
    session_id: str = "",
) -> dict:
    """Prepare the dossier's payment delays declaration (loi 69-21) in Experio for the period the client names
    (``periode``: « T3 2026 », « 2025 » for a yearly dossier; empty for the last ended): made or taken up, refreshed by
    Experio, what Experio refuses and leaves out said, its XML made. ``chiffre_affaires``: the turnover the client
    gives (Experio needs it to know the mode the law requires). ``declarer``: the client asks to declare it; declared
    only once ``client_confirmed``. The DGI deposit stays the cabinet's."""
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    with refused(
        f"prepare_payment_delay_declaration {dossier_id}",
        "Experio n'a pas pu préparer la déclaration des délais de paiement ({reason}).",
    ):
        fields = ["use_payment_delay", "payment_delay_declaration_mode"]
        company = odoo_call("res.company", "read", [dossier_id], fields=fields)[0]
        if not company["use_payment_delay"]:
            return stop(
                f"Le dossier {dossier} n'est pas soumis aux délais de paiement dans Experio (réglage « Délais de "
                "paiement » non activé) : pas de déclaration à faire."
            )
        mode = company["payment_delay_declaration_mode"] or "quarterly"
        today = date.today()
        number, year = period_of(mode, periode, today)
        period = find_period(mode, number) if number else None
        label = f"délais de paiement {period['name'] + ' ' if period else ''}{year}"
        table_id, made = declaration_of(dossier_id, period, year, context)
        if table_id is None:
            return ok(f"{TEST_MODE}La déclaration des {label} du dossier {dossier} serait créée et préparée.")
        turnover = as_number(chiffre_affaires) if str(chiffre_affaires).strip() else None
        table = odoo_call(TABLE, "read", [table_id], fields=FIELDS, context=context)[0]
        if table["state"] == "done":
            lines = odoo_call(LINE, "read", table["line_ids"], fields=LINE_FIELDS, context=context)
            return ok(
                f"Déclaration des {label} (n° {table_id}) : déjà déclarée dans Experio. {totals(table, lines)}",
                declaration_id=table_id,
            )
        if turnover and not dry_run():
            odoo_call(TABLE, "write", [table_id], {"amount_turnover": turnover}, context=context)
        lines = odoo_call(LINE, "read", table["line_ids"], fields=LINE_FIELDS, context=context)
        manual = sum(bool(line["added_manually"]) for line in lines)
        kept = ""
        if manual:  # Experio's refresh deletes them
            kept = f" Non actualisée : {manual} ligne(s) ajoutée(s) à la main, qu'« Actualiser » effacerait."
        elif not dry_run():
            odoo_json(TABLE, "action_refresh", [table_id], context=context)
        table = odoo_call(TABLE, "read", [table_id], fields=FIELDS, context=context)[0]
        table["id"] = table_id
        lines = odoo_call(LINE, "read", table["line_ids"], fields=LINE_FIELDS, context=context)
        if not table["amount_turnover"]:
            return question(
                f"Déclaration des {label} du dossier {dossier} (n° {table_id}) : Experio a besoin du chiffre "
                "d'affaires du dossier pour savoir quelle déclaration la loi 69-21 demande. Quel est-il ?",
                declaration_id=table_id,
            )
        text = f"Déclaration des {label} (n° {table_id}{', créée' if made else ''}) : {totals(table, lines)}{kept}"
        missing = left_out(dossier_id, table, context)
        if missing:
            text += " Pas dans la déclaration : " + " ; ".join(missing) + "."
        refusals = []
        if table["error_msg"]:
            refusals.append(plain(table["error_msg"]))
        if table["end_date"] and table["end_date"] >= today.isoformat():
            refusals.append("la période n'est pas terminée (Experio ne le vérifie pas)")
        overlap = [("company_id", "=", dossier_id), ("state", "=", "done"), ("id", "!=", table_id)]
        overlap += [("start_date", "<=", table["end_date"]), ("end_date", ">=", table["start_date"])]
        if table["start_date"] and odoo_call(TABLE, "search_count", overlap, context=context):
            refusals.append("la période est déjà déclarée")
        partners = sorted({line["partner_id"][0] for line in lines if line["partner_id"]})
        if partners:
            bare = odoo_call(
                "exp.partner",
                "search_read",
                [("id", "in", partners), ("vat", "=", False)],
                fields=["name"],
                context=context,
            )
            if bare:
                refusals.append("fournisseur(s) sans IF : " + ", ".join(p["name"] for p in bare))
        if refusals:
            return stop(
                f"{text} Experio ne la laissera pas déclarer : " + " ; ".join(refusals) + ".", declaration_id=table_id
            )
        if lines and not dry_run():
            try:  # Experio's own checks of its XML (the dossier's IF, its period)
                odoo_json(TABLE, "print_xml_deduction_report", [table_id], context=context)
            except ExperioRefusal as error:
                return stop(
                    f"{text} Experio refuse son fichier XML : {experio_refusal(error)}", declaration_id=table_id
                )
            name = keep_record_file(session_id, TABLE, table_id, "datas", "datas_fname", context)
            text += f" Fichier XML `{name}` joint à cette réponse." if name else " Fichier XML prêt sur la déclaration."
        if not declarer:
            return ok(
                text + " Écrivez « déclarez les délais de paiement » pour la déclarer dans Experio.",
                declaration_id=table_id,
            )
        if not client_confirmed:
            return question(
                f"{text} Je la déclare dans Experio ? Le dépôt sur le portail de la DGI restera à faire par "
                "le cabinet. Je le fais ?",
                declaration_id=table_id,
            )
        if dry_run():
            return ok(f"{TEST_MODE}La déclaration des {label} serait déclarée.", declaration_id=table_id)
        try:
            odoo_json(TABLE, "action_post", [table_id], context=context)
        except ExperioRefusal as error:
            return stop(
                f"Déclaration des {label} : pas déclarée, Experio refuse : {experio_refusal(error)}",
                declaration_id=table_id,
            )
        return ok(
            f"Déclaration des {label} déclarée dans Experio. Reste au cabinet : le dépôt sur le portail de la "
            "DGI (délais de paiement).",
            declaration_id=table_id,
        )
