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
            return [{"declaration_mode": "quarterly", "regime_type": "encaissement"}]
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
            raise ExperioRefusal(state["refusal"])
        if method == "action_post":
            state["table"].update(state="done", name="TVA/Trimestre 3/2026")
            return state["post"]
        return None

    monkeypatch.setattr(tva, "odoo_call", odoo_call)
    monkeypatch.setattr(tva, "odoo_json", odoo_json)
    monkeypatch.setattr(tva, "dry_run", lambda: False)
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
    assert result["status"] == "ok" and "TVA à payer 1200.00" in result["message"]
    assert "action_print_xml_for_deduction_report" in methods(experio)
    assert "action_post" not in methods(experio)  # never declared without the client's words


@pytest.mark.functionality
def test_what_the_declaration_leaves_out_is_told(experio):
    experio["done_before"] = 1  # a later declaration: Experio carries the credit
    experio["counts"] = {"draft": 2, "blocked": 1, "unpaid": 4, "suspense": 3}
    message = tva.prepare_tva_declaration(857)["message"]
    assert "2 facture(s) de la période encore en brouillon" in message
    assert "1 facture(s) bloquée(s)" in message
    assert "4 facture(s) d'achat non payée(s)" in message
    assert "3 opération(s) bancaire(s)" in message


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
    assert asked["status"] == "question" and "Je le fais ?" in asked["message"]
    assert "action_post" not in methods(experio)
    done = tva.prepare_tva_declaration(857, declarer=True, client_confirmed=True)
    assert ("action_post", True) in experio["calls"]  # its coherence findings were in the question
    assert done["status"] == "ok" and "dépôt sur le portail de la DGI" in done["message"]


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
    assert "Déjà déclarée" in result["message"] and methods(experio) == []
