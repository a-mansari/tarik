"""A TVA declaration (déclaration de TVA), prepared as the cabinet does it on Experio's own screen (module declaration,
exp.deduction.table), declared only when the client asks and confirms.

- Period: the one the client names, else the last one ended for the dossier's declaration mode, as Experio's form
  picks it (exp_declaration_base._onchange_fiscal_year_auto_select_period). A dossier « hors champ » declares none.
- The declaration: the dossier's draft of that period, else a new one; its lines made by Experio's « Actualiser »
  (action_refresh, which RPC's create doesn't run).
- Crédit de TVA reporté (tva_en_rouge): Experio carries it from the dossier's last declared period; for its first
  declaration in Experio it is 0 and « Actualiser » writes 0 over any value, so the client's figure is asked once and
  written after each refresh.
- Before declaring, what the declaration leaves out without a word (drafts of the period, blocked invoices, unpaid
  purchases: no lettered payment, no deduction), Experio's own checks (check_declaration_requirements), the lines in
  error, and its coherence screen's findings (bank operations in suspense, tiers to verify) are told; the XML is made
  on the declaration when the checks pass.
- « Déclarer » (action_post: TVA writings, period locked) only on the client's words, after a confirmation. The
  deposit on the DGI's portal stays a person's act: Tarik never logs in there, and never cancels a declaration.
"""

import re
from datetime import date

from engine.tools.core.app import ToolError, ok, question, step_tool, stop
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
from engine.tools.core.words import as_number, normalized

TABLE = "exp.deduction.table"
LINE = "exp.deduction.table.line"
PERIOD = "exp.declaration.period"
MONTHS = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre")
MONTHS += ("novembre", "decembre")
FIELDS = ["state", "period_id", "fiscal_year", "start_date", "end_date", "regime", "tva_en_rouge", "tva_due"]
FIELDS += ["deduction_amount", "tax_purchase_amount", "imposable_amount", "ca_total", "count_deduction_ids"]
FIELDS += ["count_imposable_ids", "error_msg", "warning_msg", "config_error_msg", "datas_fname", "name"]


def last_ended(mode, today):
    """(period number, year) of the last period ended at ``today``, as Experio's form picks it: the month before,
    or the quarter before (January: the year before's last)."""
    if mode == "monthly":
        return (today.month - 1, today.year) if today.month > 1 else (12, today.year - 1)
    quarter = (today.month - 1) // 3
    return (quarter, today.year) if quarter else (4, today.year - 1)


def period_named(mode, words, today):
    """(period number, year) the client's ``words`` name (« T3 », « 3e trimestre 2026 », « août », « 08/2026 »),
    None when they name none; ToolError when they name a month for a quarterly dossier or the reverse."""
    words = normalized(words or "")
    if not words:
        return None
    year = re.search(r"(?<!\d)(20\d\d)(?!\d)", words)
    year = int(year.group(1)) if year else None
    quarter = re.search(r"(?<![a-z0-9])(?:t|q)([1-4])(?![0-9])|(?<!\d)([1-4]) ?(?:er|e|eme)? trimestre", words)
    month = next(
        (number for number, name in enumerate(MONTHS, 1) if re.search(rf"(?<![a-z]){name}(?![a-z])", words)), None
    )
    numeric = re.search(r"(?<![a-z0-9])(0?[1-9]|1[0-2]) (20\d\d)(?!\d)", words)  # « 08/2026 », normalized
    if numeric:
        month, year = int(numeric.group(1)), int(numeric.group(2))
    if quarter:
        number = int(quarter.group(1) or quarter.group(2))
        if mode == "monthly":
            raise ToolError("Ce dossier déclare sa TVA chaque mois : quel mois déclarer ?")
    elif month:
        number = month
        if mode == "quarterly":
            raise ToolError("Ce dossier déclare sa TVA chaque trimestre : quel trimestre déclarer (T1 à T4) ?")
    else:
        return None
    return number, year or today.year


def find_period(mode, number):
    found = odoo_call(
        PERIOD, "search_read", [("type", "=", mode), ("period_id", "=", number)], fields=["name"], limit=1
    )
    if not found:
        raise ToolError(f"Experio n'a pas de période {number} ({mode}).")
    return found[0]


def declaration_of(dossier_id, period, year, regime, context):
    """The dossier's declaration of the period: its declared one, else its draft, else a new draft (None in test
    mode). (id or None, made now)."""
    domain = [("company_id", "=", dossier_id), ("period_id", "=", period["id"]), ("fiscal_year", "=", str(year))]
    domain += [("state", "!=", "cancel")]
    found = odoo_call(TABLE, "search_read", domain, fields=["state"], order="state desc, id desc", context=context)
    done = [table for table in found if table["state"] == "done"]
    if done or found:
        return (done or found)[0]["id"], False
    if dry_run():
        return None, False
    values = {
        "company_id": dossier_id,
        "period_id": period["id"],
        "fiscal_year": str(year),
        "regime": regime,
        "declaration_type": "normal",
        "date_declaration": date.today().isoformat(),  # Experio's default is the server's start day
    }
    return odoo_call(TABLE, "create", values, context=context), True


def is_first(dossier_id, table, context):
    """True when the dossier has no declared TVA period before this one in Experio (no carried credit)."""
    domain = [("company_id", "=", dossier_id), ("state", "=", "done"), ("end_date", "<", table["start_date"])]
    return not odoo_call(TABLE, "search_count", domain, context=context)


def left_out(dossier_id, table, context):
    """What the period's declaration leaves out without a word: [« n description »]."""
    period = [("company_id", "=", dossier_id), ("invoice_date", ">=", table["start_date"])]
    period += [("invoice_date", "<=", table["end_date"])]
    said = []
    drafts = odoo_call("exp.invoice", "search_count", [*period, ("state", "=", "draft")], context=context)
    if drafts:
        said.append(f"{drafts} facture(s) de la période encore en brouillon (comptabilisez-les d'abord)")
    blocked = odoo_call(
        "exp.invoice", "search_count", [*period, ("declaration_state", "=", "blocked")], context=context
    )
    if blocked:
        said.append(f"{blocked} facture(s) bloquée(s) pour la déclaration dans Experio")
    if table["regime"] == "encaissement":
        unpaid = [
            ("type", "=", "purchase"),
            ("state", "in", ["post", "export"]),
            ("payment_state", "in", ["unpaid", "partial"]),
        ]
        count = odoo_call("exp.invoice", "search_count", [*period, *unpaid], context=context)
        if count:
            said.append(f"{count} facture(s) d'achat non payée(s) ou en partie (TVA déductible à son paiement lettré)")
    return said


def suspense_count(dossier_id, table, context):
    """The bank operations of the period Experio's coherence screen flags (get_bank_lines_in_suspense's domain:
    unfinished, or posted without tiers, or a posted purchase/sale not reconciled)."""
    domain = [
        ("company_id", "=", dossier_id),
        ("date", ">=", table["start_date"]),
        ("date", "<=", table["end_date"]),
        "|",
        ("state", "in", ["draft", "ready", "suspense", "pending"]),
        "&",
        "&",
        ("state", "in", ["post", "export"]),
        ("operation_type", "in", ["purchase", "sale", "purchase_advance", "sale_advance"]),
        "|",
        ("partner_id", "=", False),
        "&",
        ("operation_type", "in", ["purchase", "sale"]),
        ("is_reconciled", "=", False),
    ]
    return odoo_call("exp.bank.statement.line", "search_count", domain, context=context)


def line_errors(table_id, context):
    """{Experio's error on a line: how many lines}."""
    domain = [("error_msg", "!=", False), "|", ("deduction_id", "=", table_id), ("imposable_id", "=", table_id)]
    errors = {}
    for line in odoo_call(LINE, "search_read", domain, fields=["error_msg"], context=context):
        reason = plain(line["error_msg"])
        errors[reason] = errors.get(reason, 0) + 1
    return errors


def check(table_id, context):
    """Experio's own checks before declaring (check_declaration_requirements: the company's IF, the period ended and
    single, the lines' errors, the carried credit...): its refusal in its words, else ""."""
    try:
        odoo_json(TABLE, "check_declaration_requirements", [table_id], context=context)
    except ExperioRefusal as error:
        return experio_refusal(error)
    return ""


def summary(table, label):
    due = round(table["tva_due"] or 0, 2)
    owed = f"TVA à payer {due:.2f}" if due > 0 else f"crédit de TVA {abs(due):.2f}"
    return (
        f"Déclaration de TVA {label} (n° {table['id']}, régime {table['regime']}) : {owed} ; TVA facturée "
        f"{table['imposable_amount'] or 0:.2f}, TVA déductible {table['tax_purchase_amount'] or 0:.2f}, crédit "
        f"reporté {table['tva_en_rouge'] or 0:.2f}, chiffre d'affaires {table['ca_total'] or 0:.2f} ; "
        f"{table['count_imposable_ids']} ligne(s) de ventes, {table['count_deduction_ids']} de déductions."
    )


def read_table(table_id, context):
    table = odoo_call(TABLE, "read", [table_id], fields=FIELDS, context=context)[0]
    table["id"] = table_id
    return table


@step_tool
def prepare_tva_declaration(
    dossier_id: int,
    periode: str = "",
    credit_reporte: str = "",
    declarer: bool = False,
    client_confirmed: bool = False,
    session_id: str = "",
) -> dict:
    """Prepare the dossier's TVA declaration in Experio for the period the client names (``periode``: « T3 2026 »,
    « août », empty for the last period ended): made or taken up, its lines refreshed by Experio, what it leaves out
    and Experio's checks said, its XML made when they pass. ``credit_reporte``: the TVA credit carried from before
    Experio, asked on the dossier's first declaration. ``declarer``: the client asks to declare it; it is declared
    (Experio's « Déclarer »: TVA writings, period locked) only once ``client_confirmed``. The DGI deposit stays
    the cabinet's."""
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    with refused(f"prepare_tva_declaration {dossier_id}", "Experio n'a pas pu préparer la déclaration ({reason})."):
        company = odoo_call("res.company", "read", [dossier_id], fields=["declaration_mode", "regime_type"])[0]
        mode = company["declaration_mode"]
        if mode not in ("monthly", "quarterly"):
            return stop(f"Le dossier {dossier} est hors champ de la TVA dans Experio : pas de déclaration à faire.")
        today = date.today()
        number, year = period_named(mode, periode, today) or last_ended(mode, today)
        period = find_period(mode, number)
        label = f"{period['name']} {year}"
        table_id, made = declaration_of(dossier_id, period, year, company["regime_type"] or "encaissement", context)
        if table_id is None:
            return ok(f"{TEST_MODE}La déclaration de TVA {label} du dossier {dossier} serait créée et préparée.")
        table = read_table(table_id, context)
        if table["state"] == "done":
            return ok(f"Déjà déclarée dans Experio. {summary(table, label)}", declaration_id=table_id)
        first = is_first(dossier_id, table, context)
        given = as_number(credit_reporte) if str(credit_reporte).strip() else None
        carried = given if given is not None else (table["tva_en_rouge"] if first else None)
        if not dry_run():
            odoo_json(TABLE, "action_refresh", [table_id], context=context)  # Experio's « Actualiser »
            if first and carried:  # « Actualiser » writes 0 on a first declaration
                odoo_call(TABLE, "write", [table_id], {"tva_en_rouge": carried}, context=context)
        table = read_table(table_id, context)
        if first and given is None and not table["tva_en_rouge"]:
            return question(
                f"Déclaration de TVA {label} du dossier {dossier} (n° {table_id}) : c'est sa première déclaration "
                "dans Experio. Quel crédit de TVA reste reporté de la période précédente (0 s'il n'y en a pas) ?",
                declaration_id=table_id,
            )
        missing = left_out(dossier_id, table, context)
        suspense = suspense_count(dossier_id, table, context)
        errors = line_errors(table_id, context)
        refusal = check(table_id, context)
        text = ("Créée. " if made else "") + summary(table, label)
        if missing:
            text += " Pas dans la déclaration : " + " ; ".join(missing) + "."
        if suspense:
            text += f" {suspense} opération(s) bancaire(s) de la période en suspens ou sans tiers (à régler d'abord)."
        if errors:
            text += " Lignes en erreur : " + " ; ".join(f"{why} ({n})" for why, n in errors.items()) + "."
        if table["warning_msg"]:
            text += f" Experio signale : {plain(table['warning_msg'])}"
        if refusal:
            return stop(f"{text} Experio ne la laisse pas déclarer : {refusal}", declaration_id=table_id)
        if not dry_run():
            odoo_json(TABLE, "action_print_xml_for_deduction_report", [table_id], context=context)  # stored on it
            text += " Le fichier XML est prêt sur la déclaration (Export XML)."
        if not declarer:
            return ok(text + " Écrivez « déclarez la TVA » pour la déclarer dans Experio.", declaration_id=table_id)
        return declare(table, label, text, client_confirmed, context)


def declare(table, label, text, client_confirmed, context):
    """Experio's « Déclarer » on the declaration prepared, once the client confirmed: its TVA writings settings first,
    its coherence screen passed (the client read its findings in the confirmation)."""
    if table["config_error_msg"]:
        return stop(
            f"{text} Pas déclarée : réglage des écritures de TVA manquant dans Experio : "
            f"{plain(table['config_error_msg'])}",
            declaration_id=table["id"],
        )
    if not client_confirmed:
        return question(
            f"{text} Je la déclare dans Experio (écritures de TVA passées, période verrouillée) ? Le dépôt sur le "
            "portail de la DGI restera à faire par le cabinet. Je le fais ?",
            declaration_id=table["id"],
        )
    if dry_run():
        return ok(f"{TEST_MODE}La déclaration de TVA {label} serait déclarée.", declaration_id=table["id"])
    try:
        action = odoo_json(TABLE, "action_post", [table["id"]], context={**context, "exp_skip_coherence_check": True})
    except ExperioRefusal as error:
        return stop(f"Déclaration de TVA {label} : pas déclarée, Experio refuse : {experio_refusal(error)}")
    if isinstance(action, dict) and action.get("res_model"):
        return stop(
            f"Déclaration de TVA {label} : pas déclarée, Experio demande une confirmation à l'écran "
            f"(« {action.get('name') or action['res_model']} ») : à valider par le cabinet dans Experio.",
            declaration_id=table["id"],
        )
    done = read_table(table["id"], context)
    return ok(
        f"Déclaration de TVA {label} déclarée dans Experio ({done['name']}), écritures de TVA passées. "
        "Reste au cabinet : le dépôt sur le portail de la DGI (fichier XML de la déclaration).",
        declaration_id=table["id"],
    )
