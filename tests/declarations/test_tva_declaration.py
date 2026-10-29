"""A TVA declaration: its period, made or taken up and refreshed by Experio, what it leaves out told, its carried
credit asked on a first declaration, declared only on the client's words and confirmation."""

from datetime import date

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.experio import ExperioRefusal
from engine.tools.declarations import tva


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("mode", "today", "expected"),
    [
        ("quarterly", date(2026, 10, 25), (3, 2026)),
        ("quarterly", date(2026, 2, 1), (4, 2025)),
        ("monthly", date(2026, 10, 25), (9, 2026)),
        ("monthly", date(2026, 1, 5), (12, 2025)),
    ],
)
def test_the_last_period_ended_is_experios_own_pick(mode, today, expected):
    assert tva.last_ended(mode, today) == expected


@pytest.mark.functionality
def test_the_period_the_client_names_is_read():
    today = date(2026, 10, 25)
    assert tva.period_named("quarterly", "la TVA du T3", today) == (3, 2026)
    assert tva.period_named("quarterly", "2e trimestre 2025", today) == (2, 2025)
    assert tva.period_named("monthly", "août 2026", today) == (8, 2026)
    assert tva.period_named("monthly", "08/2026", today) == (8, 2026)
    assert tva.period_named("monthly", "", today) is None
    with pytest.raises(ToolError, match="chaque trimestre"):
        tva.period_named("quarterly", "août", today)


@pytest.fixture
def experio(monkeypatch):
    state = {
        "tables": [],
        "table": {
            "state": "draft",
            "period_id": [3, "Trimestre 3"],
            "fiscal_year": "2026",
            "start_date": "2026-07-01",
            "end_date": "2026-09-30",
            "regime": "encaissement",
            "tva_en_rouge": 0.0,
            "tva_due": 1200.0,
            "deduction_amount": 300.0,
            "tax_purchase_amount": 300.0,
            "imposable_amount": 1500.0,
            "ca_total": 7500.0,
            "count_deduction_ids": 2,
            "count_imposable_ids": 3,
            "error_msg": False,
            "warning_msg": False,
            "config_error_msg": False,
            "datas_fname": False,
            "name": "/",
            "declaration_type": "normal",
        },
        "done_before": 0,
        "counts": {"draft": 0, "blocked": 0, "unpaid": 0, "suspense": 0},
        "written": [],
        "calls": [],
        "refusal": None,
        "post": None,
    }

    def odoo_call(model, method, *args, **kw):
        if model == "res.company":
            return [{"declaration_mode": "quarterly", "regime_type": "encaissement", "vat": "IF-1"}]
        if model == tva.PERIOD:
            return [{"id": 3, "name": "Trimestre 3"}]
        if model == tva.TABLE and method == "search_read":
            return state["tables"]
        if model == tva.TABLE and method == "create":
            state["calls"].append(("create", args[0]))
            return 50
        if model == tva.TABLE and method == "read":
            return [dict(state["table"])]
        if model == tva.TABLE and method == "write":
            state["written"].append(args[1])
            state["table"].update(args[1])
            return True
        if model == tva.TABLE and method == "search_count":
            return state["done_before"]
        if model == "exp.invoice":
            domain = args[0]
            if ("state", "=", "draft") in domain:
                return state["counts"]["draft"]
            if ("declaration_state", "=", "blocked") in domain:
                return state["counts"]["blocked"]
            return state["counts"]["unpaid"]
        if model == "exp.bank.statement.line":
            return state["counts"]["suspense"]
        return []

    def odoo_json(model, method, ids, **kw):
        state["calls"].append((method, kw.get("context", {}).get("exp_skip_coherence_check")))
        if method == "action_refresh":
            state["table"]["tva_en_rouge"] = 0.0  # Experio writes 0 on a first declaration
        if method == "check_declaration_requirements" and state["refusal"]:
            raise ExperioRefusal(state["refusal"], "odoo.exceptions.UserError")
        if method == "action_post":
            state["table"].update(state="done", name="TVA/Trimestre 3/2026")
            return state["post"]
        return None

    monkeypatch.setattr(tva, "odoo_call", odoo_call)
    monkeypatch.setattr(tva, "odoo_json", odoo_json)
    monkeypatch.setattr(tva, "dry_run", lambda: False)
    monkeypatch.setattr(
        tva,
        "keep_record_file",
        lambda session, model, record, field, name_field, context: (
            state.setdefault("kept", []).append((session, model, field)) or "Releve_Deductions_Trimestre 3_2026.zip"
        ),
    )
    monkeypatch.setattr(tva, "require_dossier", lambda dossier_id: "TARIK TEST")
    return state


def methods(state):
    return [call[0] for call in state["calls"]]


@pytest.mark.functionality
def test_a_first_declaration_is_made_refreshed_and_asks_the_carried_credit(experio):
    result = tva.prepare_tva_declaration(857, "T3 2026")
    assert result["status"] == "question" and "crédit de TVA" in result["message"]
    assert methods(experio)[:2] == ["create", "action_refresh"]  # Experio's « Actualiser » makes the lines


@pytest.mark.functionality
def test_the_carried_credit_given_is_written_after_experios_refresh(experio):
    experio["tables"] = [{"id": 50, "state": "draft"}]
    result = tva.prepare_tva_declaration(857, "T3 2026", credit_reporte="1 250,00")
    assert experio["written"] == [{"tva_en_rouge": 1250.0}]
    assert result["status"] == "ok" and "TVA à payer : 1200.00" in result["message"]
    assert "action_print_xml_for_deduction_report" in methods(experio)
    assert experio["kept"] == [("", tva.TABLE, "datas")] and "joint à cette réponse" in result["message"]
    assert "action_post" not in methods(experio)  # never declared without the client's words


@pytest.mark.functionality
def test_what_the_declaration_leaves_out_is_told(experio):
    experio["done_before"] = 1  # a later declaration: Experio carries the credit
    experio["counts"] = {"draft": 2, "blocked": 1, "unpaid": 4, "suspense": 3}
    message = tva.prepare_tva_declaration(857)["message"]
    assert "2 factures de la période sont encore en brouillon" in message
    assert "1 facture est bloquée" in message
    assert "4 factures d'achat ne sont pas payées" in message
    assert "3 opérations bancaires de la période sont en suspens" in message


@pytest.mark.functionality
def test_experios_refusal_stops_before_any_file(experio):
    experio["done_before"] = 1
    experio["refusal"] = "Vous ne pouvez pas déclarer cette période car elle n'est pas encore terminée"
    result = tva.prepare_tva_declaration(857)
    assert result["status"] == "stop" and "pas encore terminée" in result["message"]
    assert "action_print_xml_for_deduction_report" not in methods(experio)


@pytest.mark.security
def test_declaring_asks_first_then_declares_on_the_clients_yes(experio):
    experio["done_before"] = 1
    asked = tva.prepare_tva_declaration(857, declarer=True)
    assert asked["status"] == "question" and "Je la déclare dans Experio ?" in asked["message"]
    assert "action_post" not in methods(experio)
    done = tva.prepare_tva_declaration(857, declarer=True, client_confirmed=True)
    assert ("action_post", True) in experio["calls"]  # its coherence findings were in the question
    assert done["status"] == "ok" and "déposer sur le portail de la DGI" in done["message"]


@pytest.mark.security
def test_missing_tva_writing_settings_stop_the_declaring(experio):
    experio["done_before"] = 1
    experio["table"]["config_error_msg"] = "<p>Le compte de TVA due n'est pas défini</p>"
    result = tva.prepare_tva_declaration(857, declarer=True, client_confirmed=True)
    assert result["status"] == "stop" and "TVA due" in result["message"]
    assert "action_post" not in methods(experio)


@pytest.mark.functionality
def test_a_declared_period_is_only_read(experio):
    experio["tables"] = [{"id": 50, "state": "done"}]
    experio["table"]["state"] = "done"
    result = tva.prepare_tva_declaration(857, "T3")
    assert "Elle est déjà déclarée dans Experio." in result["message"] and methods(experio) == []


@pytest.mark.functionality
def test_a_quarter_named_for_a_monthly_dossier_is_asked_again_and_no_period_named_is_none():
    today = date(2026, 10, 25)
    with pytest.raises(ToolError, match="chaque mois"):
        tva.period_named("monthly", "T3 2026", today)
    assert tva.period_named("quarterly", "la TVA s'il vous plaît", today) is None
    assert tva.period_named("quarterly", "3e trimestre", today) == (3, 2026)  # no year said: this one


@pytest.mark.communication
def test_a_period_experio_lacks_is_said(monkeypatch):
    monkeypatch.setattr(tva, "odoo_call", lambda *a, **k: [])
    with pytest.raises(ToolError, match="Experio n'a pas de période mensuelle n° 13"):
        tva.find_period("monthly", 13)


@pytest.mark.functionality
def test_a_dossier_outside_tva_declares_none(experio, monkeypatch):
    inner = tva.odoo_call
    company = [{"declaration_mode": "none", "regime_type": False}]
    monkeypatch.setattr(tva, "odoo_call", lambda model, *a, **k: company if model == "res.company" else inner(model))
    result = tva.prepare_tva_declaration(857)
    assert result["status"] == "stop" and "hors champ de la TVA" in result["message"] and experio["calls"] == []


@pytest.mark.security
def test_test_mode_creates_no_declaration(experio, monkeypatch):
    monkeypatch.setattr(tva, "dry_run", lambda: True)
    result = tva.prepare_tva_declaration(857, "T3 2026")
    assert result["message"].startswith("MODE TEST") and "Trimestre 3 2026" in result["message"]
    assert experio["calls"] == []


@pytest.mark.security
def test_test_mode_refreshes_writes_and_declares_nothing(experio, monkeypatch):
    monkeypatch.setattr(tva, "dry_run", lambda: True)
    experio["tables"] = [{"id": 50, "state": "draft"}]
    result = tva.prepare_tva_declaration(857, credit_reporte="900", declarer=True, client_confirmed=True)
    assert result["message"].startswith("MODE TEST") and "serait déclarée" in result["message"]
    assert methods(experio) == ["check_declaration_requirements"] and experio["written"] == []  # a check saves nothing


@pytest.mark.communication
def test_lines_in_error_and_experio_warning_are_told(experio, monkeypatch):
    experio["done_before"] = 1
    experio["table"]["warning_msg"] = "<p>Période <b>à vérifier</b></p>"
    errors = [{"error_msg": "<p>Taux absent</p>"}, {"error_msg": "Taux absent"}, {"error_msg": "IF manquant"}]
    inner = tva.odoo_call
    monkeypatch.setattr(
        tva,
        "odoo_call",
        lambda model, *a, **k: (
            errors if (model, k.get("fields")) == (tva.LINE, ["error_msg"]) else inner(model, *a, **k)
        ),
    )
    message = tva.prepare_tva_declaration(857)["message"]
    assert "Lignes en erreur :\n  - 2 lignes : taux absent.\n  - 1 ligne : IF manquant." in message
    assert "Experio signale : Période à vérifier" in message


def posting(experio, monkeypatch, outcome):
    experio["done_before"] = 1
    inner = tva.odoo_json

    def odoo_json(model, method, ids, **kw):
        if method == "action_post":
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return inner(model, method, ids, **kw)

    monkeypatch.setattr(tva, "odoo_json", odoo_json)
    return tva.prepare_tva_declaration(857, declarer=True, client_confirmed=True)


@pytest.mark.communication
def test_a_declaration_experio_refuses_is_said_not_declared(experio, monkeypatch):
    result = posting(experio, monkeypatch, ExperioRefusal("Période verrouillée", "odoo.exceptions.UserError"))
    assert (
        result["status"] == "stop"
        and "Experio refuse de déclarer la TVA Trimestre 3 2026 : Période verrouillée" in result["message"]
    )


@pytest.mark.security
def test_a_crash_declaring_is_not_told_as_experio_refusal(experio, monkeypatch):
    with pytest.raises(ToolError, match=r"^Échec technique, réessayez plus tard\.$"):
        posting(experio, monkeypatch, ExperioRefusal("'tax_id'", "builtins.KeyError"))


@pytest.mark.security
def test_a_window_experio_opens_at_declaring_stops_for_the_cabinet(experio, monkeypatch):
    result = posting(experio, monkeypatch, {"res_model": "exp.declaration.coherence.wizard", "name": "Cohérence"})
    assert result["status"] == "stop" and "« Cohérence »" in result["message"]


@pytest.mark.communication
def test_experio_unreachable_says_the_tva_declaration_could_not_be_prepared(experio, monkeypatch):
    def down(*args, **kw):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(tva, "odoo_call", down)
    with pytest.raises(ToolError, match=r"^Échec technique, réessayez plus tard\.$"):
        tva.prepare_tva_declaration(857)


@pytest.mark.functionality
def test_operations_already_in_the_declaration_are_not_counted_in_suspense(monkeypatch):
    asked = {}

    def odoo_call(model, method, domain, **kw):
        if model == "exp.deduction.table.line":
            asked["lines"] = domain
            return [{"bank_statement_line_id": [31, "VIR"]}, {"bank_statement_line_id": False}]
        asked["operations"] = domain
        return 2

    monkeypatch.setattr(tva, "odoo_call", odoo_call)
    table = {"id": 50, "start_date": "2026-07-01", "end_date": "2026-09-30"}
    assert tva.suspense_count(857, table, {}) == 2
    assert ("deduction_id", "=", 50) in asked["lines"] and ("imposable_id", "=", 50) in asked["lines"]
    assert asked["operations"][-1] == ("id", "not in", [31])


@pytest.mark.security
def test_a_routine_never_takes_up_a_draft_someone_works_on(experio):
    experio["done_before"] = 1
    experio["tables"] = [{"id": 50, "state": "draft"}]
    result = tva.prepare_tva_declaration(857, new_only=True)
    assert result["status"] == "ok" and result["message"].endswith(
        "Elle était déjà préparée : je ne l'ai pas actualisée."
    )
    assert methods(experio) == []  # no « Actualiser », no XML: the draft is left as it is


@pytest.mark.functionality
def test_a_routine_makes_and_prepares_a_declaration_not_there_yet(experio):
    experio["done_before"] = 1
    result = tva.prepare_tva_declaration(857, new_only=True)
    assert methods(experio)[:2] == ["create", "action_refresh"] and result["status"] == "ok"
    assert "action_post" not in methods(experio)


@pytest.mark.functionality
def test_a_routine_leaves_a_dossiers_first_declaration_to_the_cabinet(experio):
    result = tva.prepare_tva_declaration(857, new_only=True)  # no declared one before: its carried credit is asked
    assert result["status"] == "stop" and "sa première dans Experio" in result["message"] and methods(experio) == []


@pytest.mark.functionality
def test_a_routine_makes_no_declaration_experio_would_refuse_nor_tells_of_one_out_of_field(experio, monkeypatch):
    inner = tva.odoo_call
    company = {"declaration_mode": "quarterly", "regime_type": "encaissement", "vat": False}
    read = lambda model, *a, **k: [company] if model == "res.company" else inner(model, *a, **k)  # noqa: E731
    monkeypatch.setattr(tva, "odoo_call", read)
    result = tva.prepare_tva_declaration(857, new_only=True)
    assert result["status"] == "stop" and "pas d'IF" in result["message"] and methods(experio) == []
    company["declaration_mode"] = "out_of_field"
    assert tva.prepare_tva_declaration(857, new_only=True)["routine"]  # nothing due: nothing said in a report
