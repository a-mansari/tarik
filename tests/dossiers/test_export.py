"""The export of a dossier's entries: Experio's own export, its period and format, analysis unless an integration is
asked and confirmed, Experio's tiers warning asked, its totals said, its file kept for the reply."""

import base64

import pytest

from engine.tools.core.experio import ExperioRefusal
from engine.tools.dossiers import export


@pytest.mark.functionality
def test_the_period_named_becomes_dates():
    assert export.dates_named("T3 2026") == ("2026-07-01", "2026-09-30")
    assert export.dates_named("février 2028") == ("2028-02-01", "2028-02-29")
    assert export.dates_named("2026") == ("2026-01-01", "2026-12-31")
    assert export.dates_named("du 01/07/2026 au 15/08/2026") == ("2026-07-01", "2026-08-15")
    assert export.dates_named("") == (None, None)


@pytest.fixture
def experio(monkeypatch):
    state = {"created": [], "writes": [], "calls": [], "kept": [], "tiers": 0, "refusal": None}

    def odoo_call(model, method, *args, **kw):
        if model == "exp.journal":
            return [1, 2]
        if model == export.WIZARD and method == "create":
            state["created"].append(args[0])
            return 9
        if model == export.WIZARD and method == "write":
            state["writes"].append(args[1])
            return True
        if model == export.WIZARD and method == "read":
            if kw.get("fields") == ["datas"]:
                return [{"datas": base64.b64encode(b"file").decode()}]
            return [
                {
                    "date_from": "2026-07-01",
                    "date_to": "2026-09-30",
                    "export_template_id": [28, "SAGE 100"],
                    "partner_export_template_id": False,
                }
            ]
        if model == export.TIERS_WIZARD:
            return [{"missing_count": state["tiers"]}]
        if model == "exp.move.line":
            return [
                {"move_id": [1, "A"], "debit": 100.0, "credit": 0.0},
                {"move_id": [1, "A"], "debit": 0.0, "credit": 100.0},
            ]
        if model == "exp.export.template":
            return [{"id": 29, "name": "MAROSOFT"}, {"id": 28, "name": "SAGE 100"}]
        return []

    def odoo_json(model, method, ids, **kw):
        context = kw.get("context", {})
        state["calls"].append((method, context.get("exp_export_tiers_confirmed")))
        if method == "get_line_ids":
            return "exp.move.line(11, 12)"
        if state["refusal"]:
            raise ExperioRefusal(state["refusal"])
        if state["tiers"] and not context.get("exp_export_tiers_confirmed"):
            return {"res_model": export.TIERS_WIZARD, "res_id": 4}
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/exp.export.wizard/9/datas/TARIK%20TEST%20-%20Journals",
        }

    monkeypatch.setattr(export, "odoo_call", odoo_call)
    monkeypatch.setattr(export, "odoo_json", odoo_json)
    monkeypatch.setattr(export, "dry_run", lambda: False)
    monkeypatch.setattr(export, "require_dossier", lambda dossier_id: "TARIK TEST")
    monkeypatch.setattr(export, "keep_file", lambda session, name, data: state["kept"].append((session, name, data)))
    return state


@pytest.mark.functionality
def test_an_analysis_export_gives_its_totals_and_keeps_its_file(experio):
    result = export.export_entries(857, periode="T3 2026", modele="marosoft", session_id="s")
    created = experio["created"][0]
    assert created["integration"] is False and created["export_template_id"] == 29
    assert created["date_from"] == "2026-07-01" and created["journal_ids"] == [(6, 0, [1, 2])]
    assert "1 écriture(s), 2 ligne(s), débit 100.00, crédit 100.00" in result["message"]
    assert experio["kept"] == [("s", "TARIK TEST - Journals.xlsx", b"file")]
    assert "rien n'est verrouillé" in result["message"]


@pytest.mark.functionality
def test_a_text_export_takes_the_separator_named(experio):
    export.export_entries(857, format_fichier="texte avec ;", session_id="s")
    created = experio["created"][0]
    assert created["exp_export_file_type"] == "text" and created["exp_text_file_separator"] == ";"
    assert experio["kept"][0][1].endswith(".zip")


@pytest.mark.security
def test_an_integration_locks_only_once_confirmed(experio):
    asked = export.export_entries(857, integration=True, session_id="s")
    assert asked["status"] == "question" and "ne seront plus modifiables" in asked["message"]
    assert experio["writes"] == [] and experio["kept"] == []
    done = export.export_entries(857, integration=True, client_confirmed=True, session_id="s")
    assert experio["writes"] == [{"integration": True}] and "marquées exportées" in done["message"]


@pytest.mark.communication
def test_tiers_without_their_account_are_asked_then_exported_anyway(experio):
    experio["tiers"] = 3
    asked = export.export_entries(857, session_id="s")
    assert asked["status"] == "question" and "3 ligne(s) ont un tiers sans compte tiers" in asked["message"]
    export.export_entries(857, client_confirmed=True, session_id="s")
    assert ("print_excel_report", True) in experio["calls"] and experio["kept"]


@pytest.mark.functionality
def test_experios_refusal_is_said_in_its_words(experio):
    experio["refusal"] = "Veuillez sélectionner un modèle de partenaire."
    result = export.export_entries(857, quoi="les tiers", session_id="s")
    assert result["status"] == "stop" and "modèle de partenaire" in result["message"]
    assert experio["created"][0]["export_type"] == "partner"
