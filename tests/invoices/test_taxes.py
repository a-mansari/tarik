"""The dossier's tax for a TVA rate it has none for, made as the cabinet's training team makes it."""

import pytest

from engine.tools.invoices import taxes

WATER = {  # a water bill at 10 %, in a dossier made from a FEC: TVA 0 % and 20 % purchase taxes only
    "type": "purchase",
    "company_id": [902, "Naruto SARL"],
    "account_id": [243203, "61250000 Achats non stockes"],
    "folder_id": False,
    "invoice_type": "normal",
    "is_local_invoice": True,
    "retenue": False,
    "amount_tax_non_recoverable": False,
    **{f"amount_tva_{rate}": 0.0 for rate in taxes.RATES},
    "amount_tva_10": 4.18,
    "amount_untaxed": 41.8,
    "amount_tva": 4.18,
}


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: the dossier's taxes and settings, and what was created."""
    state = {"taxes": [{"id": 4951, "rate": 20.0, "account_id": [243204, "34550000 État - TVA récupérable"]}]}
    state.update(created=[], listed=[4949, 4951], dry=False)

    def odoo_call(model, method, *args, **kw):
        if model == "exp.invoice":
            return [WATER]
        if (model, method) == ("exp.tax", "search_read"):
            return state["taxes"]
        if (model, method) == ("exp.tax.code", "search"):
            return {150: [76], 146: [74]}.get({d[0]: d[2] for d in args[0]}["code"], [])
        if (model, method) == ("exp.tax", "create"):
            state["created"].append(args[0])
            return 5000
        if (model, method) == ("res.company", "read"):
            return [{"purchase_exploit_tax_ids": list(state["listed"])}]
        if (model, method) == ("res.config.settings", "create"):
            state["listed"] = args[0]["purchase_exploit_tax_ids"][0][2]
            return 1
        raise AssertionError(f"unexpected call {model}.{method}")

    monkeypatch.setattr(taxes, "odoo_call", odoo_call)
    monkeypatch.setattr(taxes, "dry_run", lambda: state["dry"])
    state["odoo_call"] = odoo_call
    return state


@pytest.mark.functionality
def test_a_missing_rate_gets_its_tax_on_the_dossiers_tva_account(experio):
    notes = taxes.add_missing_taxes(272379, {})
    assert experio["created"] == [
        {
            "name": "TVA 10% ACHATS",
            "type": "purchase",
            "is_immo_tax": False,
            "rate": 10,
            "account_id": 243204,
            "code_id": 76,  # DGI 150, achat a l'interieur at 10 %
            "account_prefix": "34550",
            "account_length": 8,
            "company_id": 902,
        }
    ]
    assert experio["listed"] == [4949, 4951, 5000]  # Configuration > General > Achats > Autres taxes
    assert notes == [
        "taxe `TVA 10% ACHATS` créée (compte `34550000`)",
        "taxe 10 % ajoutée aux taxes du dossier (Configuration > Général)",
    ]


@pytest.mark.functionality
def test_a_rate_with_its_listed_tax_changes_nothing(experio):
    experio["taxes"].append({"id": 4960, "rate": 10.0, "account_id": [243204, "34550000 État"]})
    experio["listed"].append(4960)
    assert taxes.add_missing_taxes(272379, {}) == [] and experio["created"] == []


@pytest.mark.functionality
def test_in_test_mode_nothing_is_created(experio):
    experio["dry"] = True
    assert taxes.add_missing_taxes(272379, {}) == ["taxe `TVA 10% ACHATS` serait créée (compte `34550000`)"]
    assert experio["created"] == []


@pytest.mark.functionality
def test_the_dgi_code_follows_the_cabinets_table(experio):
    assert taxes.dgi_code("purchase", 20, False, "61110000") == 74  # 146, achat a l'interieur
    assert taxes.dgi_code("purchase", 10, True, "61110000") is None  # 149: not in this fake Experio
    assert taxes.DGI_CODES[("purchase", 10, True)] == 149
    assert taxes.dgi_code("purchase", 14, False, "61110000") is None  # not in the table: no code


@pytest.mark.functionality
@pytest.mark.parametrize(
    "amounts, untaxed, tva, rates",
    [
        ({10: 4.18}, 41.8, 4.18, [10]),
        ({10: 10.0, 20: 40.0}, 300.0, 50.0, [10, 20]),  # a multi-rate invoice whose rates add up
        ({3: 3.22, 20: 260.0}, 1300.0, 260.0, [20]),  # B6: a stray 3.22 under 3 % never makes a 3 % tax
        ({3: 3.22}, 1710.0, 0.0, []),  # a foreign invoice with no TVA: none
    ],
)
def test_a_tax_is_made_only_for_a_rate_the_invoice_really_has(amounts, untaxed, tva, rates):
    invoice = {**{f"amount_tva_{rate}": 0.0 for rate in taxes.RATES}, "amount_untaxed": untaxed, "amount_tva": tva}
    invoice.update({f"amount_tva_{rate}": amount for rate, amount in amounts.items()})
    assert taxes.rates_used(invoice) == rates


@pytest.mark.functionality
def test_a_sale_without_tva_gets_experios_own_zero_rate_tax_listed(monkeypatch):
    """No TVA (or TVA non facturée): Experio books it on its side's 0 % tax; Tarik lists Experio's own, made by
    Experio (create_tax_ids) when missing, never one of its own."""
    sale = {**WATER, "type": "sale", "account_id": [1, "71240000 Prestations"], "amount_tva": 0.0}
    sale.update(amount_tva_10=0.0, amount_untaxed=500.0)
    state = {"zero": [], "listed": [4973], "made": []}

    def odoo_call(model, method, *args, **kw):
        if model == "exp.invoice":
            return [sale]
        if (model, method) == ("exp.tax", "search"):
            return state["zero"]
        if (model, method) == ("exp.tax", "search_read"):
            return []
        if (model, method) == ("exp.account", "search"):
            return [31]
        if (model, method) == ("res.company", "read"):
            return [{"sale_tax_ids": list(state["listed"])}]
        if (model, method) == ("res.config.settings", "create"):
            state["listed"] = args[0]["sale_tax_ids"][0][2]
            return 1
        raise AssertionError(f"unexpected call {model}.{method}")

    def odoo_json(model, method, ids, context):
        state["made"].append((method, ids))
        state["zero"] = [4972]  # Experio's « TVA 0% VENTES »

    monkeypatch.setattr(taxes, "odoo_call", odoo_call)
    monkeypatch.setattr(taxes, "odoo_json", odoo_json)
    monkeypatch.setattr(taxes, "dry_run", lambda: False)
    assert taxes.add_missing_taxes(5, {}) == ["taxe 0 % ajoutée aux taxes du dossier (Configuration > Général)"]
    assert state["made"] == [("create_tax_ids", [31])] and state["listed"] == [4973, 4972]
    assert taxes.add_missing_taxes(5, {}) == [] and len(state["made"]) == 1  # there and listed: nothing done
    assert not taxes.needs_zero_rate({**sale, "type": "purchase", "is_local_invoice": False})  # foreign purchase


@pytest.mark.functionality
def test_without_a_tax_of_its_kind_the_tva_account_is_found_by_its_code(monkeypatch):
    accounts = {
        "34552%": [],
        "3455%": [{"id": 8, "code": "34550000"}],
        "34551%": [{"id": 9, "code": "34551000"}],
        "4455%": [{"id": 7, "code": "44550000"}],
    }
    searched = []

    def odoo_call(model, method, domain, fields, order, limit):
        searched.append(domain)
        return accounts[domain[1][2]]

    monkeypatch.setattr(taxes, "odoo_call", odoo_call)
    assert taxes.tva_account(5, "purchase", False, []) == {"id": 8, "code": "34550000"}  # 34552 first, then 3455
    assert [domain[1][2] for domain in searched] == ["34552%", "3455%"]
    assert ("code", "not like", "34551%") in searched[1]  # never an immobilisation's for a charge
    assert taxes.tva_account(5, "purchase", True, []) == {"id": 9, "code": "34551000"}
    assert taxes.tva_account(5, "sale", False, []) == {"id": 7, "code": "44550000"}
    accounts["3455%"] = []
    assert taxes.tva_account(5, "purchase", False, []) is None


def dossier_without_taxes(monkeypatch, invoice, accounts=()):
    """A fake Experio: ``invoice``, a dossier with no tax at all and only ``accounts``; creating one fails the test."""

    def odoo_call(model, method, *args, **kw):
        if model == "exp.invoice":
            return [invoice]
        if (model, method) in (("exp.tax", "search_read"), ("exp.tax", "search"), ("exp.account", "search_read")):
            return []
        if (model, method) == ("exp.account", "search"):
            return list(accounts)
        raise AssertionError(f"unexpected call {model}.{method}")

    monkeypatch.setattr(taxes, "odoo_call", odoo_call)
    monkeypatch.setattr(taxes, "odoo_json", lambda *a, **kw: pytest.fail("Experio makes no tax without its account"))


@pytest.mark.communication
def test_a_dossier_without_any_tva_account_is_left_to_the_cabinet(monkeypatch):
    monkeypatch.setattr(taxes, "dry_run", lambda: False)
    dossier_without_taxes(monkeypatch, WATER)
    assert taxes.add_missing_taxes(272379, {}) == [
        "taxe 10 % : aucun compte de TVA dans le dossier, à créer par le cabinet"
    ]
    sale = {**WATER, "type": "sale", "account_id": [1, "71240000 Prestations"], "amount_tva": 0.0}
    sale.update(amount_tva_10=0.0, amount_untaxed=500.0)
    dossier_without_taxes(monkeypatch, sale)
    assert taxes.add_missing_taxes(5, {}) == [
        "taxe 0 % : aucun compte de TVA dans le dossier pour la créer, à créer par le cabinet"
    ]


@pytest.mark.communication
def test_in_test_mode_a_missing_zero_rate_tax_is_never_said_to_lack_its_tva_account(monkeypatch):
    monkeypatch.setattr(taxes, "dry_run", lambda: True)
    sale = {**WATER, "type": "sale", "account_id": [1, "71240000 Prestations"], "amount_tva": 0.0}
    sale.update(amount_tva_10=0.0, amount_untaxed=500.0)
    dossier_without_taxes(monkeypatch, sale, accounts=[31])
    assert not any("aucun compte de TVA" in note for note in taxes.add_missing_taxes(5, {}))


@pytest.mark.security
def test_a_tax_the_cabinets_rights_keep_out_of_the_settings_is_left_to_the_cabinet(experio, monkeypatch):
    fake = taxes.odoo_call
    monkeypatch.setattr(
        taxes, "odoo_call", lambda model, *a, **kw: 9 if model == "res.config.settings" else fake(model, *a, **kw)
    )
    assert taxes.add_missing_taxes(272379, {}) == [
        "taxe `TVA 10% ACHATS` créée (compte `34550000`)",
        "taxe 10 % à ajouter par le cabinet dans Configuration > Général (autres taxes)",
    ]
    assert experio["listed"] == [4949, 4951]


@pytest.mark.security
def test_a_zero_rate_tax_the_cabinets_rights_keep_out_is_left_to_the_cabinet(monkeypatch):
    sale = {**WATER, "type": "sale", "account_id": [1, "71240000 Prestations"], "amount_tva": 0.0}
    sale.update(amount_tva_10=0.0, amount_untaxed=500.0)

    def odoo_call(model, method, *args, **kw):
        if model == "exp.invoice":
            return [sale]
        if (model, method) == ("exp.tax", "search"):
            return [4972]  # Experio's own « TVA 0% VENTES »
        if (model, method) == ("exp.tax", "search_read"):
            return []
        if (model, method) == ("res.company", "read"):
            return [{"sale_tax_ids": [4973]}]  # the settings' form saves nothing: the cabinet's rights
        if (model, method) == ("res.config.settings", "create"):
            return 1
        raise AssertionError(f"unexpected call {model}.{method}")

    monkeypatch.setattr(taxes, "odoo_call", odoo_call)
    monkeypatch.setattr(taxes, "dry_run", lambda: False)
    assert taxes.add_missing_taxes(5, {}) == [
        "taxe 0 % à ajouter par le cabinet dans Configuration > Général (autres taxes)"
    ]
    monkeypatch.setattr(taxes, "dry_run", lambda: True)
    assert taxes.add_missing_taxes(5, {}) == []  # test mode: the settings never written


@pytest.mark.functionality
def test_in_test_mode_an_existing_tax_is_never_listed(experio):
    experio["dry"] = True
    experio["taxes"].append({"id": 4960, "rate": 10.0, "account_id": [243204, "34550000 État"]})
    assert taxes.add_missing_taxes(272379, {}) == [] and experio["listed"] == [4949, 4951]


@pytest.mark.functionality
def test_a_foreign_class_2_purchase_gets_the_ordinary_tax_and_a_withheld_one_none(experio, monkeypatch):
    """Experio's immobilisation taxes are a local purchase's only; a foreign purchase with retenue takes its own."""
    machine = {**WATER, "account_id": [9, "23400000 Materiel"], "is_local_invoice": False}
    monkeypatch.setattr(taxes, "odoo_call", lambda *a, **k: [machine] if a[0] == "exp.invoice" else inner(*a, **k))
    inner = experio["odoo_call"]
    taxes.add_missing_taxes(272379, {})
    assert experio["created"][0]["is_immo_tax"] is False and experio["created"][0]["name"] == "TVA 10% ACHATS"
    machine["retenue"] = True
    experio["created"].clear()
    assert taxes.add_missing_taxes(272379, {}) == [] and experio["created"] == []
