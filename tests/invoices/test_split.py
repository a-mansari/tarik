"""A draft's charges split over several expense accounts through Experio's own wizard (the real calls are faked)."""

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
    """split_over_accounts on DRAFT 3 holding the untaxed ``lines``: the calls made to Experio, in order."""
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
    said = split.split_over_accounts(3, {20.0: [(31, 700.0), (32, 300.0)]})
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
def test_each_rate_is_split_on_its_own_line(splitting):
    splitting["lines"] = [LINE, {**LINE, "id": 71, "tax_rate": 10.0, "amount_ht": 500.0, "amount_tva": 50.0}]
    split.split_over_accounts(3, {20.0: [(31, 700.0), (32, 300.0)], 10.0: [(31, 200.0), (32, 300.0)]})
    opened = [call[2] for call in splitting["calls"] if call[1] == "action_open_split_wizard"]
    assert opened == [[70], [71]]


@pytest.mark.functionality
def test_the_first_part_takes_the_tva_rounding_so_the_lines_keep_the_invoices_tva(splitting):
    splitting["lines"][0].update(amount_ht=100.01, amount_tva=20.0)  # 20 % of each third rounds to 6.67, not 20
    split.split_over_accounts(3, {20.0: [(31, 33.34), (32, 33.34), (31, 33.33)]})
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
        split.split_over_accounts(3, {20.0: parts})
    assert all(call[1] != "write" for call in splitting["calls"])


@pytest.mark.security
def test_nothing_is_written_when_one_rate_cannot_be_split(splitting):
    splitting["lines"] = [LINE, {**LINE, "id": 71, "tax_rate": 10.0, "amount_ht": 500.0, "amount_tva": 50.0}]
    with pytest.raises(ToolError, match="aucune ligne de charges à 7.0 %"):
        split.split_over_accounts(3, {20.0: [(31, 700.0), (32, 300.0)], 7.0: [(31, 1.0), (32, 1.0)]})
    assert all(call[1] not in ("write", "action_open_split_wizard") for call in splitting["calls"])


@pytest.mark.functionality
@pytest.mark.parametrize("lines", [[], [LINE, {**LINE, "id": 71}]])
def test_a_rate_without_exactly_one_charge_line_is_left_to_the_cabinet(splitting, lines):
    splitting["lines"] = [dict(line) for line in lines]
    with pytest.raises(ToolError, match="le cabinet la répartit dans Experio"):
        split.split_over_accounts(3, {20.0: [(31, 700.0), (32, 300.0)]})


@pytest.mark.security
def test_test_mode_says_the_split_and_writes_nothing(splitting, monkeypatch):
    monkeypatch.setattr(split, "dry_run", lambda: True)
    assert "MODE TEST" in split.split_over_accounts(3, {20.0: [(31, 700.0), (32, 300.0)]}).upper()
    assert all(call[1] not in ("write", "validate_lines", "action_open_split_wizard") for call in splitting["calls"])


def charges(*lines):
    """Return scan charge lines ``lines`` ((designation, HT, rate)) as the check stores them."""
    return [{"designation": d, "amount_ht": ht, "rate": rate} for d, ht, rate in lines]


@pytest.mark.functionality
def test_charge_lines_are_grouped_by_rate_when_they_add_up_to_experios_line():
    untaxed = [LINE, {**LINE, "id": 71, "tax_rate": 10.0, "amount_ht": 500.0}]
    groups, left = split.charge_groups(
        charges(("loyer", 600, 20), ("charges", 400, 20), ("parking", 500, 10), ("x", 1, 7)), untaxed
    )
    assert groups == {20: [("loyer", 600), ("charges", 400)]} and left == []  # one line at 10: nothing to split
    groups, left = split.charge_groups(charges(("loyer", 600, 20), ("charges", 300, 20)), untaxed)
    assert groups == {} and left == ["les lignes à 20 % ne font pas le HT de la facture"]
    groups, left = split.charge_groups(charges(("loyer", 600, 20), ("charges", 400.04, 20)), untaxed)
    assert list(groups) == [20]  # a rounding the largest part takes
    groups, left = split.charge_groups(charges(("loyer", 600, 20), ("charges", 400, 20)), [LINE, {**LINE, "id": 9}])
    assert groups == {} and left == ["à 20 % Experio a 2 lignes de charges"]  # already split
    groups, left = split.charge_groups(charges(("loyer", 600, 20), ("", 400, 20)), untaxed)
    assert groups == {} and left == ["les lignes du scan ne sont pas toutes lisibles"]


@pytest.fixture
def stepping(monkeypatch):
    """split_charges on DRAFT 3: the model's account per designation, the lines Experio holds, the split made."""
    state = {"charges": charges(("loyer", 600, 20), ("transport", 400, 20)), "made": [], "untaxed": [dict(LINE)]}
    accounts = {
        "loyer": {"id": 31, "code": "6131", "name": "Locations"},
        "transport": {"id": 32, "code": "6145", "name": "Transports"},
        "machine": {"id": 33, "code": "2332", "name": "Matériel"},
    }
    state["accounts"] = accounts
    monkeypatch.setattr(split, "require_invoice", lambda *a: dict(DRAFT))
    monkeypatch.setattr(split, "stored_check", lambda i: (True, {"charge_lines": state["charges"]}, []))
    monkeypatch.setattr(split, "side_of", lambda invoice: "purchase")
    monkeypatch.setattr(split, "untaxed_lines", lambda i, c: state["untaxed"])
    monkeypatch.setattr(split, "invoice_context", lambda invoice: {})
    monkeypatch.setattr(split, "dry_run", lambda: False)
    monkeypatch.setattr(split, "test_mode_note", lambda: "")
    monkeypatch.setattr(split, "model_choice", lambda invoice, kind, designation: (accounts.get(designation), "x"))

    def made(invoice_id, by_rate):
        state["made"].append(by_rate)
        return "fait"

    monkeypatch.setattr(split, "split_over_accounts", made)
    return state


@pytest.mark.functionality
def test_an_invoice_billing_things_of_different_kinds_is_split_on_the_model_s_accounts(stepping):
    result = split.split_charges(3)
    assert stepping["made"] == [{20: [(31, 600), (32, 400)]}]
    assert result["status"] == "ok" and not result.get("routine")
    assert result["message"] == (
        "Facture 3 : charges réparties à 20 % : 600.00 en `6131 Locations`, 400.00 en `6145 Transports`."
    )


@pytest.mark.functionality
def test_lines_the_model_puts_on_one_account_stay_one_line_and_a_rounding_goes_to_the_largest(stepping, monkeypatch):
    stepping["charges"] = charges(("loyer", 300, 20), ("loyer", 300.03, 20), ("transport", 400, 20))
    split.split_charges(3)
    assert stepping["made"] == [{20: [(31, 600.0), (32, 400.0)]}]  # 300 + 300.03 merged, then 600.03 less 0.03
    stepping["made"].clear()
    stepping["charges"] = charges(("loyer", 600, 20), ("loyer", 400, 20))
    result = split.split_charges(3)
    assert stepping["made"] == [] and result["routine"] and "un seul compte suffit" in result["message"]


@pytest.mark.security
@pytest.mark.parametrize(
    ("lines", "why"),
    [
        (charges(("loyer", 600, 20), ("inconnu", 400, 20)), "aucun compte sûr pour « inconnu »"),
        (charges(("loyer", 600, 20), ("machine", 400, 20)), "« machine » ressemble à une immobilisation"),
    ],
)
def test_a_line_without_a_sure_account_or_a_durable_good_leaves_the_invoice_on_one_account(stepping, lines, why):
    stepping["charges"] = lines
    result = split.split_charges(3)
    assert stepping["made"] == [] and result["routine"] and why in result["message"]


@pytest.mark.functionality
def test_one_line_a_posted_invoice_or_no_check_changes_nothing(stepping, monkeypatch):
    stepping["charges"] = charges(("loyer", 1000, 20))
    assert split.split_charges(3)["routine"]
    monkeypatch.setattr(split, "stored_check", lambda i: None)
    assert split.split_charges(3)["routine"]
    monkeypatch.setattr(split, "require_invoice", lambda *a: {**DRAFT, "state": "post"})
    assert "pas un brouillon" in split.split_charges(3)["message"]
    assert stepping["made"] == []


@pytest.mark.functionality
def test_a_split_experio_refuses_leaves_the_invoice_on_its_account_and_says_so(stepping, monkeypatch):
    def refuse(invoice_id, by_rate):
        raise ToolError("Facture 3 : Experio a refusé la répartition.")

    monkeypatch.setattr(split, "split_over_accounts", refuse)
    result = split.split_charges(3)
    assert (
        result["status"] == "ok"
        and "Experio a refusé la répartition. Charges laissées sur un seul compte." in (result["message"])
    )
