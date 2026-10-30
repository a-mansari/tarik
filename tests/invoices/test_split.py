"""A draft's HT split over several expense accounts, through Experio's own wizard (the real calls are faked)."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.invoices import split

LINE = {
    "id": 70,
    "line_type": "untaxed",
    "amount_ht": 1000.0,
    "amount_tva": 200.0,
    "tax_id": [12, "TVA 20%"],
    "tax_rate": 20.0,
    "code_id": [5, "code"],
    "account_id": [31, "6111"],
}
DRAFT = {"id": 3, "state": "draft", "company_id": [5, "D"], "exercice_id": [2, "2026"], "type": "purchase"}


@pytest.fixture
def splitting(monkeypatch):
    """split_over_accounts on DRAFT 3 holding one untaxed LINE: the calls made to Experio, in order."""
    state = {"lines": [dict(LINE)], "calls": [], "codes": {31: "6111", 32: "6145", 40: "3421"}}

    def odoo_call(model, method, ids, *args, **kw):
        state["calls"].append((model, method, ids, args))
        if model == "exp.invoice":
            return [{"tva_line_ids": [line["id"] for line in state["lines"]]}]
        if model == "exp.invoice.line":
            return state["lines"]
        if model == "exp.account":
            return [{"id": i, "code": state["codes"][i]} for i in ids]
        return None

    def odoo_json(model, method, ids, **kw):
        state["calls"].append((model, method, ids, ()))
        return {"res_id": 9} if method == "action_open_split_wizard" else None

    monkeypatch.setattr(split, "require_draft_invoice", lambda *a: DRAFT)
    monkeypatch.setattr(split, "odoo_call", odoo_call)
    monkeypatch.setattr(split, "odoo_json", odoo_json)
    monkeypatch.setattr(split, "dry_run", lambda: False)
    return state


@pytest.mark.functionality
def test_the_ht_is_split_through_experios_wizard_each_part_at_the_lines_tax(splitting):
    said = split.split_over_accounts(3, [(31, 700.0), (32, 300.0)])
    assert said == "Facture 3 : charges réparties sur 2 comptes (6111, 6145)."
    opened, written, applied = splitting["calls"][-3:]
    assert opened[:3] == ("exp.invoice.line", "action_open_split_wizard", [70])
    assert written[:3] == ("exp.invoice.line.split", "write", [9])
    parts = written[3][0]["line_ids"]
    assert parts[0] == [5, 0, 0]
    assert [p[2] for p in parts[1:]] == [
        {"account_id": 31, "amount_ht": 700.0, "amount_tva": 140.0, "tax_id": 12, "code_id": 5},
        {"account_id": 32, "amount_ht": 300.0, "amount_tva": 60.0, "tax_id": 12, "code_id": 5},
    ]
    assert applied[:3] == ("exp.invoice.line.split", "validate_lines", [9])


@pytest.mark.functionality
def test_the_first_part_takes_the_tva_rounding_so_the_lines_keep_the_invoices_tva(splitting):
    splitting["lines"][0].update(amount_ht=100.01, amount_tva=20.0)  # 20 % of each third rounds to 6.67, not 20
    split.split_over_accounts(3, [(31, 33.34), (32, 33.34), (31, 33.33)])
    amounts = [p[2]["amount_tva"] for p in splitting["calls"][-2][3][0]["line_ids"][1:]]
    assert amounts == [6.66, 6.67, 6.67] and round(sum(amounts), 2) == 20.0


@pytest.mark.security
@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ([(31, 1000.0)], "au moins deux comptes"),
        ([(31, 1100.0), (32, -100.0)], "au moins deux comptes"),
        ([(31, 700.0), (32, 200.0)], "900.00 HT, la facture en compte 1000.00"),  # never a guessed remainder
        ([(31, 700.0), (40, 300.0)], "compte 3421 hors des classes 2/6 d'un achat"),
    ],
)
def test_a_split_that_does_not_add_up_or_leaves_the_class_is_refused_before_anything_is_written(
    splitting, parts, message
):
    with pytest.raises(ToolError, match=message):
        split.split_over_accounts(3, parts)
    assert all(call[1] != "write" for call in splitting["calls"])


@pytest.mark.functionality
@pytest.mark.parametrize("lines", [[], [LINE, {**LINE, "id": 71}]])
def test_an_invoice_without_exactly_one_charge_line_is_left_to_the_cabinet(splitting, lines):
    splitting["lines"] = [dict(line) for line in lines]
    with pytest.raises(ToolError, match="le cabinet la répartit dans Experio"):
        split.split_over_accounts(3, [(31, 700.0), (32, 300.0)])


@pytest.mark.security
def test_test_mode_says_the_split_and_writes_nothing(splitting, monkeypatch):
    monkeypatch.setattr(split, "dry_run", lambda: True)
    assert "MODE TEST" in split.split_over_accounts(3, [(31, 700.0), (32, 300.0)]).upper()
    assert all(call[1] not in ("write", "validate_lines", "action_open_split_wizard") for call in splitting["calls"])
