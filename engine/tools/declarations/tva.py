"""A TVA declaration (déclaration de TVA), prepared as the cabinet does it on Experio's own screen (module declaration,
exp.deduction.table), declared only when the client asks and confirms.

- Period: the one the client names, else the last one ended for the dossier's declaration mode, as Experio's form
  picks it (exp.declaration.base._onchange_fiscal_year_auto_select_period). A dossier « hors champ » declares none.
- The declaration: the dossier's declared one or draft of that period, else a new one; its lines made by Experio's
  « Actualiser » (action_refresh, which keeps the lines added by hand; an RPC create runs no onchange).
- Crédit de TVA reporté (tva_en_rouge): Experio carries it from the dossier's last declared period (get_tva_en_rouge);
  for its first declaration in Experio it is 0 and « Actualiser » writes 0 over any value, so the client's figure is
  asked once and written after each refresh.
- Before declaring, reported to the client: what the declaration leaves out without a word (drafts of the period,
  blocked invoices, unpaid purchases under the encaissement regime: no lettered payment, no deduction), Experio's own
  checks (check_declaration_requirements), the lines in error, and the bank operations in suspense its coherence
  screen flags (get_bank_lines_in_suspense; its other findings are not reported). The XML is made on the declaration
  when the checks pass.
- « Déclarer » (action_post: TVA entry made, the declared invoices and lines flagged) only on the client's words,
  after a confirmation, with the coherence screen skipped (exp_skip_coherence_check). The deposit on the DGI's portal
  stays a person's act: Tarik never logs in there, and never cancels a declaration.
"""

import re
from datetime import date

from engine.tools.core.app import ToolError, ok, question, routine, step_tool, stop
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    ExperioRefusal,
    dry_run,
    experio_refusal,
    in_dossier,
    is_refusal,
    odoo_call,
    odoo_json,
    plain,
    refused,
    require_dossier,
)
from engine.tools.core.outbox import keep_record_file
from engine.tools.core.words import as_number, counted, normalized, reasons

TABLE = "exp.deduction.table"
LINE = "exp.deduction.table.line"
PERIOD = "exp.declaration.period"
MONTHS = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre")
MONTHS += ("novembre", "decembre")
FIELDS = ["state", "period_id", "fiscal_year", "start_date", "end_date", "regime", "tva_en_rouge", "tva_due"]
FIELDS += ["deduction_amount", "tax_purchase_amount", "imposable_amount", "ca_total", "count_deduction_ids"]
FIELDS += ["count_imposable_ids", "error_msg", "warning_msg", "config_error_msg", "datas_fname", "name"]
FIELDS += ["declaration_type"]
MODES = {"monthly": "mensuelle", "quarterly": "trimestrielle"}


def last_ended(mode, today):
    """Return (period number, year) of the last period ended at ``today`` in ``mode`` ("monthly" or "quarterly").

    The month before, or the quarter before; in January (or Q1) the last of the year before, as Experio picks it.
    """
    if mode == "monthly":
        return (today.month - 1, today.year) if today.month > 1 else (12, today.year - 1)
    quarter = (today.month - 1) // 3
    return (quarter, today.year) if quarter else (4, today.year - 1)


def period_named(mode, words, today):
    """Return (period number, year) the client's ``words`` name (« T3 », « 3e trimestre 2026 », « août », « 08/2026 »).

    The year defaults to ``today``'s. Returns None when the words name no period.

    Raises:
        ToolError: a month named for a quarterly dossier, or a quarter for a monthly one.
    """
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
    """Return Experio's period record {"id", "name"} of type ``mode`` and number ``number``.

    Raises:
        ToolError: Experio has no such period (exp.declaration.period).
    """
    found = odoo_call(
        PERIOD, "search_read", [("type", "=", mode), ("period_id", "=", number)], fields=["name"], limit=1
    )
    if not found:
        raise ToolError(f"Experio n'a pas de période {MODES.get(mode, mode)} n° {number}.")
    return found[0]


def declaration_of(dossier_id, period, year, regime, context):
    """Find or create the dossier's TVA declaration for ``period`` and ``year``; a new one gets ``regime``.

    Returns (declaration_id, created): the declared one, else the latest draft, with created False; else a new draft
    with created True. (None, False) in test mode when there is none to take up.
    """
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
    """True when the dossier has no declared TVA declaration of ``table``'s type ending before ``table`` starts (no
    carried credit): get_tva_en_rouge's own search (exp_deduction_table.py)."""
    domain = [("company_id", "=", dossier_id), ("state", "=", "done"), ("end_date", "<", table["start_date"])]
    domain.append(("declaration_type", "=", table["declaration_type"]))
    return not odoo_call(TABLE, "search_count", domain, context=context)


def left_out(dossier_id, table, context):
    """Return French phrases counting what the period's declaration leaves out without a word; [] when nothing."""
    period = [("company_id", "=", dossier_id), ("invoice_date", ">=", table["start_date"])]
    period += [("invoice_date", "<=", table["end_date"])]
    said = []
    drafts = odoo_call("exp.invoice", "search_count", [*period, ("state", "=", "draft")], context=context)
    if drafts:
        said.append(
            counted(
                drafts,
                "{} facture de la période est encore en brouillon : comptabilisez-la d'abord",
                "{} factures de la période sont encore en brouillon : comptabilisez-les d'abord",
            )
        )
    blocked = odoo_call(
        "exp.invoice", "search_count", [*period, ("declaration_state", "=", "blocked")], context=context
    )
    if blocked:
        said.append(
            counted(
                blocked,
                "{} facture est bloquée pour la déclaration dans Experio",
                "{} factures sont bloquées pour la déclaration dans Experio",
            )
        )
    if table["regime"] == "encaissement":
        unpaid = [
            ("type", "=", "purchase"),
            ("state", "in", ["post", "export"]),
            ("payment_state", "in", ["unpaid", "partial"]),
        ]
        count = odoo_call("exp.invoice", "search_count", [*period, *unpaid], context=context)
        if count:
            said.append(
                counted(
                    count,
                    "{} facture d'achat n'est pas payée ou l'est en partie : sa TVA sera déductible quand son paiement "
                    "sera lettré",
                    "{} factures d'achat ne sont pas payées ou le sont en partie : leur TVA sera déductible quand leur "
                    "paiement sera lettré",
                )
            )
    return said


def suspense_count(dossier_id, table, context):
    """Return how many bank operations of the period Experio's coherence screen flags.

    Same as get_bank_lines_in_suspense: unfinished, or posted without tiers, or a posted purchase/sale not reconciled,
    except the operations already in the declaration (its deduction_ids and imposable_ids lines' bank operations).
    """
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
    declared = [  # deduction_ids | imposable_ids, their One2many domains (exp_deduction_table.py)
        "|",
        "&",
        "&",
        "&",
        ("deduction_id", "=", table["id"]),
        ("line_category", "=", "deduction"),
        ("is_previous_period", "=", False),
        ("exp_deleted", "=", False),
        "&",
        "&",
        ("imposable_id", "=", table["id"]),
        ("line_category", "=", "imposable"),
        ("exp_deleted", "=", False),
    ]
    lines = odoo_call(LINE, "search_read", declared, fields=["bank_statement_line_id"], context=context)
    inside = [line["bank_statement_line_id"][0] for line in lines if line["bank_statement_line_id"]]
    return odoo_call("exp.bank.statement.line", "search_count", domain + [("id", "not in", inside)], context=context)


def line_errors(table_id, context):
    """Return {Experio's error on a line, in plain text: how many lines of the declaration have it}."""
    domain = [("error_msg", "!=", False), "|", ("deduction_id", "=", table_id), ("imposable_id", "=", table_id)]
    errors = {}
    for line in odoo_call(LINE, "search_read", domain, fields=["error_msg"], context=context):
        reason = plain(line["error_msg"])
        errors[reason] = errors.get(reason, 0) + 1
    return errors


def check(table_id, context):
    """Run Experio's own checks before declaring and return its refusal in its words, else "".

    check_declaration_requirements: the company's IF, the period ended and not already declared, the lines' errors,
    a negative carried credit, the non-resident and RAS TVA client lines.
    """
    try:
        odoo_json(TABLE, "check_declaration_requirements", [table_id], context=context)
    except ExperioRefusal as error:
        if not is_refusal(error):  # a crash: told as one (refused(), calls.call), never as Experio's reason
            raise
        return experio_refusal(error)
    return ""


def summary(table, label, made=False):
    """Return the French summary of the declaration ``table``: a headline (« créée » when ``made``), then its TVA due
    or credit, amounts and line counts, one per indented « - » line."""
    due = round(table["tva_due"] or 0, 2)
    owed = f"TVA à payer : {due:.2f}" if due > 0 else f"Crédit de TVA : {abs(due):.2f}"
    regime = "débit" if table["regime"] == "debit" else table["regime"]
    sales = counted(table["count_imposable_ids"], "{} ligne de ventes", "{} lignes de ventes")
    deductions = counted(table["count_deduction_ids"], "{} ligne de déductions", "{} lignes de déductions")
    return "\n".join(
        [
            f"Déclaration de TVA {label} (n° `{table['id']}`, régime {regime}{', créée' if made else ''}) :",
            f"- {owed}",
            f"- TVA facturée : {table['imposable_amount'] or 0:.2f}",
            f"- TVA déductible : {table['tax_purchase_amount'] or 0:.2f}",
            f"- Crédit reporté : {table['tva_en_rouge'] or 0:.2f}",
            f"- Chiffre d'affaires : {table['ca_total'] or 0:.2f}",
            f"- {sales} et {deductions}.",
        ]
    )


def read_table(table_id, context):
    """Return the declaration read with FIELDS, its "id" added."""
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
    new_only: bool = False,
) -> dict:
    """Prepare the dossier's TVA declaration in Experio, and declare it when asked and confirmed.

    The declaration is created or taken up, its lines refreshed by Experio; what it leaves out and Experio's checks are
    reported to the client, and its XML is made when they pass.

    Args:
        periode: the period the client names (« T3 2026 », « août »); empty for the last period ended.
        credit_reporte: the TVA credit carried from before Experio, asked on the dossier's first declaration.
        declarer: the client asks to declare it.
        client_confirmed: the client confirmed; only then is Experio's « Déclarer » run (TVA entry made).
        new_only: a routine's unattended run: a declaration only made, never one taken up (« Actualiser » would
            rebuild a draft someone is working on, exp_deduction_table.py _onchange_fiscal_year_and_period), and none
            made for the dossier's first one (its carried credit is asked).

    Returns a stop result for a dossier « hors champ » or when Experio refuses, a question result for the carried
    credit or the confirmation, else an ok result. The DGI deposit stays the cabinet's.
    """
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    with refused(f"prepare_tva_declaration {dossier_id}", "Experio n'a pas pu préparer la déclaration ({reason})."):
        fields = ["declaration_mode", "regime_type", "vat"]
        company = odoo_call("res.company", "read", [dossier_id], fields=fields)[0]
        mode = company["declaration_mode"]
        if mode not in ("monthly", "quarterly"):
            text = f"Le dossier `{dossier}` est hors champ de la TVA dans Experio : pas de déclaration à faire."
            return routine(text) if new_only else stop(text)
        if new_only and not company["vat"]:  # Experio refuses to declare without it (check_declaration_requirements)
            return stop(f"Le dossier `{dossier}` n'a pas d'IF dans Experio : je n'ai créé aucune déclaration de TVA.")
        today = date.today()
        number, year = period_named(mode, periode, today) or last_ended(mode, today)
        period = find_period(mode, number)
        label = f"{period['name']} {year}"
        declared = [("company_id", "=", dossier_id), ("state", "=", "done")]
        if new_only and not odoo_call(TABLE, "search_count", declared, context=context):
            return stop(
                f"La déclaration de TVA {label} du dossier `{dossier}` est sa première dans Experio : préparez-la avec "
                "moi en conversation, car je dois vous demander le crédit de TVA reporté. Je n'ai rien créé."
            )
        table_id, made = declaration_of(dossier_id, period, year, company["regime_type"] or "encaissement", context)
        if table_id is None:
            return ok(f"{TEST_MODE}La déclaration de TVA {label} du dossier `{dossier}` serait créée et préparée.")
        table = read_table(table_id, context)
        if table["state"] == "done":
            text = f"{summary(table, label)}\n\nElle est déjà déclarée dans Experio."
            return ok(text, declaration_id=table_id, already=True)
        if new_only and not made:
            text = f"{summary(table, label)}\n\nElle était déjà préparée : je ne l'ai pas actualisée."
            return ok(text, declaration_id=table_id, already=True)
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
                f"Déclaration de TVA {label} du dossier `{dossier}` (n° `{table_id}`) : c'est sa première déclaration "
                "dans Experio. Quel crédit de TVA reste reporté de la période précédente (0 s'il n'y en a pas) ?",
                declaration_id=table_id,
            )
        missing = left_out(dossier_id, table, context)
        suspense = suspense_count(dossier_id, table, context)
        errors = line_errors(table_id, context)
        refusal = check(table_id, context)
        text = summary(table, label, made)
        if missing:
            text += "\n- Pas dans la déclaration :" + "".join(f"\n  - {why}." for why in missing)
        if suspense:
            text += "\n- " + counted(
                suspense,
                "{} opération bancaire de la période est en suspens ou sans tiers : à régler d'abord.",
                "{} opérations bancaires de la période sont en suspens ou sans tiers : à régler d'abord.",
            )
        if errors:
            text += "\n- Lignes en erreur :"
            text += "".join(f"\n  - {line}" for line in reasons(errors, "{} ligne", "{} lignes"))
        if table["warning_msg"]:
            text += f"\n- Experio signale : {plain(table['warning_msg'])}"
        if refusal:
            return stop(f"{text}\n\nExperio ne la laisse pas déclarer : {refusal}", declaration_id=table_id)
        if not dry_run():
            odoo_json(TABLE, "action_print_xml_for_deduction_report", [table_id], context=context)  # stored on it
            name = keep_record_file(session_id, TABLE, table_id, "datas", "datas_fname", context)
            text += (
                f"\n- Fichier XML `{name}` joint à cette réponse."
                if name
                else "\n- Fichier XML prêt sur la déclaration."
            )
        if not declarer:
            return ok(
                text + "\n\nÉcrivez « déclarez la TVA » pour que je la déclare dans Experio.", declaration_id=table_id
            )
        return declare(table, label, text, client_confirmed, context)


def declare(table, label, text, client_confirmed, context):
    """Run Experio's « Déclarer » on the prepared declaration ``table`` once ``client_confirmed``.

    Returns a stop result when the TVA entry settings are missing (config_error_msg) or Experio refuses or shows a
    screen to confirm, a question result before confirmation, else an ok result. The coherence screen is skipped
    (exp_skip_coherence_check): the client read its bank findings in the confirmation (``text``).
    """
    if table["config_error_msg"]:
        return stop(
            f"{text}\n\nJe ne l'ai pas déclarée, car un réglage des écritures de TVA manque dans Experio : "
            f"{plain(table['config_error_msg'])}",
            declaration_id=table["id"],
        )
    if not client_confirmed:
        return question(
            f"{text}\n\nEn la déclarant, je passe ses écritures de TVA et je marque ses factures et opérations comme "
            "déclarées. Le dépôt sur le portail de la DGI restera à faire par le cabinet. Je la déclare dans Experio ?",
            declaration_id=table["id"],
        )
    if dry_run():
        return ok(f"{TEST_MODE}La déclaration de TVA {label} serait déclarée.", declaration_id=table["id"])
    try:
        action = odoo_json(TABLE, "action_post", [table["id"]], context={**context, "exp_skip_coherence_check": True})
    except ExperioRefusal as error:
        if not is_refusal(error):  # a crash: told as one (refused(), calls.call), never as Experio's reason
            raise
        return stop(f"Experio refuse de déclarer la TVA {label} : {experio_refusal(error)}")
    if isinstance(action, dict) and action.get("res_model"):
        window = f"la fenêtre « {action['name']} »" if action.get("name") else "une fenêtre de confirmation"
        return stop(
            f"Je n'ai pas déclaré la TVA {label} : Experio ouvre à l'écran {window}, que le cabinet doit valider "
            "dans Experio.",
            declaration_id=table["id"],
        )
    done = read_table(table["id"], context)
    return ok(
        f"J'ai déclaré la TVA {label} dans Experio (`{done['name']}`) et passé ses écritures de TVA. Il reste au "
        "cabinet à la déposer sur le portail de la DGI, avec le fichier XML de la déclaration.",
        declaration_id=table["id"],
    )
