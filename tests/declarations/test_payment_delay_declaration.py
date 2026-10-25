"""The payment delays declaration: only for a dossier following delays, its turnover asked, lines by Experio, what
Experio refuses and leaves out told, declared only on the client's words and confirmation."""

from datetime import date

import pytest

from engine.tools.core.experio import ExperioRefusal
from engine.tools.declarations import payment_delay as delay


@pytest.mark.functionality
def test_a_yearly_dossier_declares_the_year_before():
    assert delay.period_of("yearly", "", date(2026, 10, 25)) == (None, 2025)
    assert delay.period_of("quarterly", "T2 2026", date(2026, 10, 25)) == (2, 2026)


@pytest.fixture
def experio(monkeypatch):
    state = {
        "company": {"use_payment_delay": True, "payment_delay_declaration_mode": "quarterly"},
        "tables": [],
        "table": {
            "state": "draft",
            "period_id": [3, "Trimestre 3"],
            "fiscal_year": "2026",
            "start_date": "2026-07-01",
            "end_date": "2026-09-30",
            "amount_turnover": 12000000.0,
            "amount_penalty": 386.0,
            "error_msg": False,
            "bam_rate": 2.25,
            "line_ids": [1],
            "name": "/",
        },
        "lines": [],
        "bare": [],
        "counts": {"untracked": 0, "blocked": 0, "declared": 0},
        "calls": [],
        "written": [],
    }

    def odoo_call(model, method, *args, **kw):
        if model == "res.company":
            return [dict(state["company"])]
        if model == delay.TABLE and method == "search_read":
            return state["tables"]
        if model == delay.TABLE and method == "create":
            state["calls"].append("create")
            return 312
        if model == delay.TABLE and method == "read":
            return [dict(state["table"])]
        if model == delay.TABLE and method == "write":
            state["written"].append(args[1])
            state["table"].update(args[1])
            return True
        if model == delay.TABLE and method == "search_count":
            return state["counts"]["declared"]
        if model == delay.LINE:
            return [dict(line) for line in state["lines"]]
        if model == "exp.invoice":
            if ("do_not_declare_ddp", "=", True) in args[0]:
                return state["counts"]["blocked"]
            return state["counts"]["untracked"]
        if model == "exp.partner":
            return state["bare"]
        return []

    def odoo_json(model, method, ids, **kw):
        state["calls"].append(method)
        if method == "action_post" and state.get("refusal"):
            raise ExperioRefusal(state["refusal"])

    monkeypatch.setattr(delay, "odoo_call", odoo_call)
    monkeypatch.setattr(delay, "odoo_json", odoo_json)
    monkeypatch.setattr(delay, "dry_run", lambda: False)
    monkeypatch.setattr(
        delay,
        "keep_record_file",
        lambda session, model, record, field, name_field, context: (
            state.setdefault("kept", []).append((session, model, field)) or "Delais.zip"
        ),
    )
    monkeypatch.setattr(delay, "require_dossier", lambda dossier_id: "TARIK TEST")
    monkeypatch.setattr(delay, "find_period", lambda mode, number: {"id": 3, "name": "Trimestre 3"})
    monkeypatch.setattr(delay, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 10, 25))}))
    return state


def line(**values):
    base = {"partner_id": [5, "ALPHA"], "added_manually": False, "montant_facture_ttc": 1200.0}
    return {**base, "montant_non_encore_paye": 1200.0, "montant_paye_hors_delai": 0.0, "amount_penalty": 30.0, **values}


@pytest.mark.functionality
def test_a_dossier_without_payment_delays_declares_none(experio):
    experio["company"]["use_payment_delay"] = False
    assert delay.prepare_payment_delay_declaration(857)["status"] == "stop"


@pytest.mark.functionality
def test_the_turnover_is_asked_when_experio_has_none_then_written(experio):
    experio["table"]["amount_turnover"] = 0.0
    asked = delay.prepare_payment_delay_declaration(857)
    assert asked["status"] == "question" and "chiffre d'affaires" in asked["message"]
    assert experio["calls"][:2] == ["create", "action_refresh"]
    delay.prepare_payment_delay_declaration(857, chiffre_affaires="12 000 000")
    assert experio["written"] == [{"amount_turnover": 12000000.0}]


@pytest.mark.functionality
def test_the_lines_totals_and_xml_are_given(experio):
    experio["lines"] = [
        line(),
        line(partner_id=[6, "BETA"], montant_paye_hors_delai=500.0, montant_non_encore_paye=0.0),
    ]
    result = delay.prepare_payment_delay_declaration(857)
    assert result["status"] == "ok"
    assert (
        "2 ligne(s), 2 fournisseur(s) ; non encore payé 1200.00, payé hors délai 500.00, amende 386.00"
        in (result["message"])
    )
    assert "print_xml_deduction_report" in experio["calls"] and "action_post" not in experio["calls"]


@pytest.mark.functionality
def test_what_experio_refuses_and_leaves_out_is_told(experio):
    experio["lines"] = [line()]
    experio["bare"] = [{"name": "ALPHA"}]
    experio["counts"] = {"untracked": 3, "blocked": 1, "declared": 0}
    experio["table"]["error_msg"] = "Selon la loi 69.21, pour un CA de 1500000.0, aucune déclaration n'est requise"
    result = delay.prepare_payment_delay_declaration(857)
    assert result["status"] == "stop"
    message = result["message"]
    assert "aucune déclaration n'est requise" in message and "fournisseur(s) sans IF : ALPHA" in message
    assert "3 facture(s) d'achat de la période sans suivi de délai" in message and "1 facture(s) bloquée(s)" in message


@pytest.mark.functionality
def test_lines_added_by_hand_are_never_refreshed_away(experio):
    experio["lines"] = [line(added_manually=True)]
    message = delay.prepare_payment_delay_declaration(857)["message"]
    assert "action_refresh" not in experio["calls"] and "ajoutée(s) à la main" in message


@pytest.mark.security
def test_declaring_asks_first_then_declares_on_the_clients_yes(experio):
    experio["lines"] = [line()]
    asked = delay.prepare_payment_delay_declaration(857, declarer=True)
    assert asked["status"] == "question" and "Je le fais ?" in asked["message"]
    assert "action_post" not in experio["calls"]
    done = delay.prepare_payment_delay_declaration(857, declarer=True, client_confirmed=True)
    assert "action_post" in experio["calls"] and "déclarée dans Experio" in done["message"]
