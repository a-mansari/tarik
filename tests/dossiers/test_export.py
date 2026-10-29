"""The export of a dossier's entries: Experio's own export, its period and format, analysis unless an integration is
asked and confirmed, Experio's tiers warning asked, its totals said, its file kept for the reply."""

import base64

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.experio import ExperioRefusal
from engine.tools.dossiers import export


@pytest.mark.functionality
def test_the_period_named_becomes_dates():
    assert export.dates_named("T3 2026") == ("2026-07-01", "2026-09-30")
    assert export.dates_named("février 2028") == ("2028-02-01", "2028-02-29")
    assert export.dates_named("2026") == ("2026-01-01", "2026-12-31")
    assert export.dates_named("du 01/07/2026 au 15/08/2026") == ("2026-07-01", "2026-08-15")
    assert export.dates_named("") == (None, None)
    year = export.date.today().year  # a quarter or a month without its year: this year's, never the whole exercice
    assert export.dates_named("T3") == (f"{year}-07-01", f"{year}-09-30")
    assert export.dates_named("septembre") == (f"{year}-09-01", f"{year}-09-30")


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
        if model == "exp.move.line" and method == "search":
            state["fec_domain"] = args[0]
            return [11, 12]
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
            raise ExperioRefusal(state["refusal"], "odoo.exceptions.UserError")
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
    assert "1 écriture, 2 lignes, débit 100.00, crédit 100.00" in result["message"]
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
    assert asked["status"] == "question" and "3 lignes ont un tiers sans compte tiers" in asked["message"]
    export.export_entries(857, client_confirmed=True, session_id="s")
    assert ("print_excel_report", True) in experio["calls"] and experio["kept"]


@pytest.mark.functionality
def test_experios_refusal_is_said_in_its_words(experio):
    experio["refusal"] = "Veuillez sélectionner un modèle de partenaire."
    result = export.export_entries(857, quoi="les tiers", session_id="s")
    assert result["status"] == "stop" and "modèle de partenaire" in result["message"]
    assert experio["created"][0]["export_type"] == "partner"
    # what models Experio has for the tiers (here none), so a journal export's model is never offered for them
    assert result["message"].endswith("Experio n'a aucun modèle d'export de tiers : il est à créer dans Experio.")


@pytest.mark.communication
def test_a_model_name_matching_several_export_models_is_asked(monkeypatch):
    templates = [{"id": 1, "name": "SAGE"}, {"id": 2, "name": "SAGE 100"}]
    monkeypatch.setattr(export, "odoo_call", lambda *a, **k: templates)
    with pytest.raises(ToolError, match="Plusieurs modèles d'export correspondent : SAGE, SAGE 100"):
        export.template_named("exp.export.template", "modèle sage 100", {})
    assert export.template_named("exp.export.template", "", {}) is None  # Experio's default


@pytest.mark.security
def test_test_mode_creates_no_export(experio, monkeypatch):
    monkeypatch.setattr(export, "dry_run", lambda: True)
    result = export.export_entries(857, session_id="s")
    assert result["message"].startswith("MODE TEST") and "serait préparé" in result["message"]
    assert experio["created"] == [] and experio["calls"] == [] and experio["kept"] == []


@pytest.mark.functionality
def test_a_fec_is_exported_by_experio_own_fec_as_text(experio):
    result = export.export_entries(857, quoi="le FEC", periode="2026", session_id="s")
    assert experio["created"][0]["export_type"] == "fec" and ("action_print_fec", None) in experio["calls"]
    assert experio["kept"][0][1] == "TARIK TEST - Journals.txt" and "modèle" not in result["message"]


@pytest.mark.functionality
def test_a_fec_counts_the_lines_experio_fec_takes_and_never_asks_for_an_integration(experio):
    result = export.export_entries(857, quoi="FEC", periode="T3 2026", integration=True, session_id="s")
    assert result["status"] == "ok" and "get_line_ids" not in [method for method, _ in experio["calls"]]
    assert ("parent_state", "in", ["posted", "exported"]) in experio["fec_domain"]
    assert ("date", ">=", "2026-07-01") in experio["fec_domain"] and ("journal_id", "in", [1, 2]) in experio[
        "fec_domain"
    ]
    assert "1 écriture, 2 lignes" in result["message"] and "rien n'est verrouillé" in result["message"]
    assert experio["writes"] == []  # never switched to integration


@pytest.mark.functionality
def test_a_tiers_export_takes_no_journals_and_counts_no_lines(experio):
    result = export.export_entries(857, quoi="tiers", periode="T3 2026", date_import=True, session_id="s")
    created = experio["created"][0]
    assert "journal_ids" not in created and created["create_date_from"] == "2026-07-01" and "date_from" not in created
    assert "get_line_ids" not in [method for method, _ in experio["calls"]] and "modèle aucun" in result["message"]


@pytest.mark.communication
def test_an_export_without_lines_counts_zero(monkeypatch):
    monkeypatch.setattr(export, "odoo_call", lambda *a, **k: pytest.fail("nothing to read"))
    monkeypatch.setattr(export, "odoo_json", lambda *a, **k: "exp.move.line()")
    assert export.totals("journal", 9, {}) == " : 0 écriture, 0 ligne, débit 0.00, crédit 0.00"


@pytest.mark.communication
def test_an_export_experio_makes_no_file_of_stops(experio, monkeypatch):
    inner = export.odoo_call
    monkeypatch.setattr(
        export,
        "odoo_call",
        lambda model, method, *a, **k: (
            [{"datas": False}] if k.get("fields") == ["datas"] else inner(model, method, *a, **k)
        ),
    )
    result = export.export_entries(857, session_id="s")
    assert result["status"] == "stop" and "Experio n'a produit aucun fichier" in result["message"]
    assert experio["kept"] == []


@pytest.mark.communication
def test_experio_unreachable_says_the_export_could_not_be_prepared(experio, monkeypatch):
    def down(*args, **kw):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(export, "odoo_call", down)
    with pytest.raises(ToolError, match=r"^Échec technique, réessayez plus tard\.$"):
        export.export_entries(857, session_id="s")
