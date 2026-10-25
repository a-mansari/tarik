"""The withholding declarations: which and which period, lines made as Experio makes them, what Experio would refuse
and leaves out told, declared only on the client's words and confirmation."""

from datetime import date

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.experio import ExperioRefusal
from engine.tools.declarations import ras


@pytest.mark.functionality
def test_the_declarations_are_those_named_else_those_the_dossier_files():
    flags = {"declaration_ras_tva": True, "declaration_ras_is": False, "declaration_ras_ir": True}
    assert ras.kinds_named("", flags) == ["tva", "ir"]
    assert ras.kinds_named("la RAS IS non-résidents", flags) == ["non_resident"]
    assert ras.kinds_named("RAS non résidents annuelle 2025", flags) == ["non_resident_yearly"]
    assert ras.kinds_named("RAS TVA et IS", flags) == ["tva", "is"]


@pytest.mark.functionality
def test_the_period_is_monthly_unless_a_quarter_is_named():
    today = date(2026, 10, 25)
    assert ras.period_of("tva", "", today) == ("monthly", 9, 2026)
    assert ras.period_of("is", "T3", today) == ("quarterly", 3, 2026)
    assert ras.period_of("non_resident_yearly", "", today) == ("yearly", None, 2025)  # Experio's: the year before
    with pytest.raises(ToolError, match="chaque mois"):
        ras.period_of("non_resident", "T3", today)


@pytest.fixture
def experio(monkeypatch):
    state = {
        "tables": {},  # model: [{id, state, type}]
        "table": {"state": "draft", "period_id": [9, "Septembre"], "fiscal_year": "2026", "name": "/"},
        "dates": ("2026-09-01", "2026-09-30"),
        "lines": [],
        "computed": [],
        "done": [],  # declared tables of an overlapping period: [{id, type}]
        "bare": [],
        "bank_ras": 0,
        "odd_tiers": 0,
        "calls": [],
        "xml_refusal": None,
    }

    def odoo_call(model, method, *args, **kw):
        if model == "res.company":
            return [{"declaration_ras_tva": True, "declaration_ras_is": True, "declaration_ras_ir": True}]
        if model == "exp.declaration.period":
            return [{"id": 9, "name": "Septembre"}]
        if method == "search_read" and model in {spec["model"] for spec in ras.KINDS.values()}:
            if ("state", "=", "done") in args[0]:
                return state["done"]
            return state["tables"].get(model, [])
        if method == "create":
            state["calls"].append(("create", model, args[0]))
            return 70
        if method == "read" and model in {spec["model"] for spec in ras.KINDS.values()}:
            start, end = state["dates"]
            fields = kw.get("fields", [])
            row = {**state["table"], "start_date": start, "end_date": end}
            row.update({field: [1, 2] for field in fields if field.endswith("_ids")})
            return [row]
        if method == "read":  # lines
            return [dict(line) for line in state["lines"]]
        if model == "exp.partner":
            return state["bare"]
        if model == "exp.bank.statement.line":
            return state["bank_ras"]
        if model == "exp.invoice":
            return state["odd_tiers"]
        return []

    def odoo_json(model, method, ids, *args, **kw):
        state["calls"].append((method, model, args))
        if method == "get_tax_withholding_values":
            return state["computed"]
        if method == "post_process_values":
            return args[0]
        if method in ("action_print_xml_ras_report", "action_generate_xml_ras") and state["xml_refusal"]:
            raise ExperioRefusal(state["xml_refusal"])
        return None

    monkeypatch.setattr(ras, "odoo_call", odoo_call)
    monkeypatch.setattr(ras, "odoo_json", odoo_json)
    monkeypatch.setattr(ras, "dry_run", lambda: False)
    monkeypatch.setattr(ras, "find_period", lambda mode, number: {"id": 9, "name": "Septembre"})
    monkeypatch.setattr(ras, "require_dossier", lambda dossier_id: "TARIK TEST")
    monkeypatch.setattr(ras, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 10, 25))}))
    return state


def line(**values):
    base = {"partner_id": [5, "ATLAS"], "added_manually": False, "withholding_percentage": "75"}
    base.update(expense_type="prestation", warning_msg=False, amount_ht=1000.0, amount_tva=200.0)
    return {**base, "amount_withholding": 150.0, **values}


def methods(state):
    return [call[0] for call in state["calls"]]


@pytest.mark.functionality
def test_ras_tva_lines_are_written_as_experios_form_computes_them(experio):
    experio["computed"] = [{"invoice_id": 3, "amount_ht": 1000.0, "payment_date": "2026-09-12"}]
    experio["lines"] = [line()]
    result = ras.prepare_ras_declaration(857, "RAS TVA")
    write = next(call for call in experio["calls"] if call[0] == "write")
    assert write[2][0] == {"tax_withholding_ids": [(5, 0, 0), (0, 0, experio["computed"][0])]}
    assert "1 ligne(s), HT 1000.00, TVA 200.00, retenue 150.00" in result["message"]
    assert "action_print_xml_ras_report" in methods(experio) and "action_post" not in methods(experio)


@pytest.mark.functionality
def test_a_declaration_with_lines_added_by_hand_is_not_refreshed(experio):
    experio["tables"] = {"exp.withholding.table": [{"id": 70, "state": "draft"}]}
    experio["lines"] = [line(added_manually=True, withholding_percentage="10")]
    result = ras.prepare_ras_declaration(857, "IS")
    assert "action_refresh" not in methods(experio)
    assert "ajoutée(s) à la main" in result["message"]


@pytest.mark.functionality
def test_what_experios_declare_would_refuse_is_said_first(experio):
    experio["dates"] = ("2026-10-01", "2026-10-31")  # not ended
    experio["lines"] = [line(expense_type=False)]
    experio["bare"] = [{"name": "ATLAS"}]
    result = ras.prepare_ras_declaration(857, "RAS TVA", "octobre 2026")
    assert result["status"] == "stop"
    message = result["message"]
    assert "la période n'est pas terminée" in message and "tiers sans ICE ou IF : ATLAS" in message
    assert "sans type d'activité" in message
    assert "action_print_xml_ras_report" not in methods(experio)


@pytest.mark.communication
def test_ir_refused_because_is_is_declared_is_explained(experio):
    experio["done"] = [{"id": 9, "type": "is"}]
    result = ras.prepare_ras_declaration(857, "IR")
    assert "la RAS IS de la même période est déjà déclarée" in result["message"]


@pytest.mark.communication
def test_withholdings_experio_leaves_out_are_told(experio):
    experio["bank_ras"] = 2
    experio["odd_tiers"] = 1
    message = ras.prepare_ras_declaration(857, "IS")["message"]
    assert "2 retenue(s) passée(s) sur un relevé bancaire" in message
    assert "ni société ni personne" in message


@pytest.mark.functionality
def test_experios_xml_refusal_stops(experio):
    experio["lines"] = [line()]
    experio["xml_refusal"] = "Partner 'ATLAS' must have an ICE"
    result = ras.prepare_ras_declaration(857, "IS")
    assert result["status"] == "stop" and "must have an ICE" in result["message"]


@pytest.mark.security
def test_declaring_asks_first_and_warns_that_is_blocks_ir(experio):
    experio["lines"] = [line()]
    asked = ras.prepare_ras_declaration(857, "IS", declarer=True)
    assert asked["status"] == "question" and "refusera la RAS IR" in asked["message"]
    assert "action_post" not in methods(experio)
    done = ras.prepare_ras_declaration(857, "IS", declarer=True, client_confirmed=True)
    assert "action_post" in methods(experio) and "déclarée dans Experio" in done["message"]


@pytest.mark.functionality
def test_a_dossier_filing_no_ras_is_asked_which(experio, monkeypatch):
    real = experio
    monkeypatch.setattr(
        ras,
        "odoo_call",
        lambda model, method, *a, **k: [
            dict.fromkeys(("declaration_ras_tva", "declaration_ras_is", "declaration_ras_ir"), False)
        ],
    )
    result = ras.prepare_ras_declaration(857)
    assert result["status"] == "stop" and "ne déclare aucune RAS" in result["message"] and real
