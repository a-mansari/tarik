"""A bank's notice of operation: its parts checked, its statement line found, the wizard's lines made."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.invoices import notice

FEE = {"id": 7, "type": "bank_fee", "bank_type": "frais_without_tax", "tax_id": [1, "10%"], "account_id": [1, "61471"]}
TAX = {"id": 8, "type": "bank_fee", "bank_type": "tax", "tax_id": False, "account_id": [2, "3455"]}
STAMP = {"id": 9, "type": False, "bank_type": False, "tax_id": False, "account_id": [3, "6167"]}
OPERATIONS = {7: FEE, 8: TAX, 9: STAMP}


def line(id, net, date="2026-07-01", label="VIR EMIS", **more):
    values = {
        "id": id,
        "state": "ready",
        "date": date,
        "payment_ref": label,
        "original_line_id": 0,
        "statement_id": [1, "S"],
    }
    values |= {
        "debit": -net if net < 0 else 0,
        "credit": net if net > 0 else 0,
        "currency_id": 5,
        "company_currency_id": 5,
    }
    return values | more


def wire(total=-10022, *parts):
    parts = parts or (("principal", -10000), ("commission", -20), ("tva", -2))
    return {"total": total, "date": "2026-07-01", "parts": [{"kind": k, "amount": a} for k, a in parts]}


@pytest.mark.functionality
def test_a_wire_with_its_commission_and_tva_is_applied_as_printed():
    parts, why = notice.checked_parts(wire())
    assert why == "" and [p["kind"] for p in parts] == ["principal", "commission", "tva"]


@pytest.mark.functionality
def test_a_notice_of_one_part_has_nothing_to_adjust():
    assert notice.checked_parts(wire(25000, ("principal", 25000))) == (None, "")


@pytest.mark.functionality
@pytest.mark.parametrize(
    "read, said",
    [
        (wire(-10023), "montant total"),
        (wire(-10022, ("principal", -10000), ("autre", -22)), "autre"),
        (wire(-10022, ("principal", -10000), ("principal", -22)), "principale"),
        (wire(-10002, ("principal", -10000), ("tva", -2)), "paires"),
        (wire(-10022, ("principal", -20000), ("commission", 9978)), "même côté"),
        (wire(-10022, ("principal", -20), ("commission", -9980), ("tva", -22)), "dépassent"),
        (wire(-10025, ("principal", -10000), ("commission", -20), ("tva", -5)), "10 %"),
        (
            {"total": None, "parts": [{"kind": "principal", "amount": "10 022,00"}, {"kind": "tva", "amount": 2}]},
            "lire",
        ),
        ({"total": -5, "parts": ["principal", "tva"]}, "lire"),
    ],
)
def test_a_notice_that_cannot_be_placed_is_left_to_the_cabinet(read, said):
    parts, why = notice.checked_parts(read)
    assert parts is None and said in why


@pytest.mark.functionality
def test_the_line_is_the_one_with_the_notices_net_amount():
    lines = [line(1, -10000), line(2, -10022), line(3, 10022)]
    assert notice.found_line(lines, wire())[0]["id"] == 2


@pytest.mark.functionality
def test_two_lines_of_one_amount_are_told_by_their_date_then_the_reference():
    lines = [line(1, -10022, "2026-03-01"), line(2, -10022, "2026-07-02"), line(3, -10022, "2026-07-01", "REF 123")]
    assert notice.found_line(lines, wire()) == (None, "several")
    assert notice.found_line(lines, {**wire(), "reference": "ref 123"})[0]["id"] == 3
    assert notice.found_line(lines[:2], wire())[0]["id"] == 2  # only one is near the notice's date


@pytest.mark.functionality
def test_a_line_already_adjusted_or_missing_is_told_apart():
    group = [line(1, -10000, original_line_id=1), line(2, -20, original_line_id=1), line(3, -2, original_line_id=1)]
    assert notice.found_line(group, wire()) == (None, "adjusted")
    assert notice.found_line([line(4, -5)], wire()) == (None, "none")
    assert notice.found_line([line(5, -10022, state="post")], wire()) == (None, "none")


@pytest.mark.functionality
def test_the_principal_keeps_the_rest_and_the_fees_take_their_operations():
    parts, _ = notice.checked_parts(wire())
    made, lacking = notice.wizard_lines(line(2, -10022), parts, OPERATIONS)
    assert lacking == "" and made[0] == {"debit": 10000, "credit": 0}
    assert [(m["operation_id"], m["debit"]) for m in made[1:]] == [(7, 20), (8, 2)]
    assert made[1]["tax_amount"] == 2 and "tax_amount" not in made[2]


@pytest.mark.functionality
def test_an_operation_the_dossier_lacks_stops_the_adjustment():
    parts, _ = notice.checked_parts(wire())
    made, lacking = notice.wizard_lines(line(2, -10022), parts, {7: FEE})
    assert made is None and "Taxe Bancaire" in lacking


@pytest.mark.functionality
def test_a_single_line_of_the_amount_far_from_the_notices_dates_is_not_the_one():
    assert notice.found_line([line(1, -10022, "2026-01-01")], wire()) == (None, "none")
    undated = {**wire(), "date": None}
    assert notice.found_line([line(1, -10022, "2026-01-01")], undated)[0]["id"] == 1


class Experio:
    """The calls adjusted() makes, recorded; ``fail`` is the wizard method that raises."""

    def __init__(self, fail=None, refuse_restore=False):
        self.calls, self.fail, self.refuse_restore = [], fail, refuse_restore

    def json(self, model, method, ids, context=None):
        self.calls.append((model, method))
        if method == self.fail:
            raise RuntimeError("refused")
        return {"res_id": 9}

    def call(self, model, method, *args, context=None, **kw):
        self.calls.append((model, method, context.get("ignore_bank_line_info_detection")))
        if method == "read":
            return [{"line_ids": [2, 30, 31]}]
        if self.refuse_restore and method == "unlink":
            raise RuntimeError("no")


def adjust_with(monkeypatch, experio):
    monkeypatch.setattr(notice, "odoo_json", experio.json)
    monkeypatch.setattr(notice, "odoo_call", experio.call)
    parts, _ = notice.checked_parts(wire())
    made, _ = notice.wizard_lines(line(2, -10022), parts, OPERATIONS)
    notice.adjusted(line(2, -10022), made, {})


@pytest.mark.functionality
def test_the_new_lines_are_made_without_experios_detection(monkeypatch):
    experio = Experio()
    adjust_with(monkeypatch, experio)
    assert ("bank.statement.split.wizard", "write", True) in experio.calls


@pytest.mark.functionality
def test_a_refused_validation_puts_the_line_back_as_it_was(monkeypatch):
    experio = Experio(fail="validate_lines")
    with pytest.raises(RuntimeError):
        adjust_with(monkeypatch, experio)
    assert ("exp.bank.statement.line", "unlink", None) in experio.calls  # the lines made, not the original (id 2)
    assert experio.calls[-1][:2] == ("exp.bank.statement.line", "write")


@pytest.mark.functionality
def test_a_line_that_cannot_be_put_back_is_said_half_adjusted(monkeypatch):
    with pytest.raises(ToolError, match="à moitié ajustée"):
        adjust_with(monkeypatch, Experio(fail="validate_lines", refuse_restore=True))
