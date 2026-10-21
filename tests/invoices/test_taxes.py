"""The dossier's tax for a TVA rate it has none for, made as the cabinet's training team makes it."""

import pytest

from tarik.invoices import taxes

WATER = {  # a water bill at 10 %, in a dossier made from a FEC: TVA 0 % and 20 % purchase taxes only
    "type": "purchase",
    "company_id": [902, "Naruto SARL"],
    "account_id": [243203, "61250000 Achats non stockes"],
    "folder_id": False,
    **{f"amount_tva_{rate}": 0.0 for rate in taxes.RATES},
    "amount_tva_10": 4.18,
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
        "taxe TVA 10% ACHATS créée (compte 34550000)",
        "taxe 10 % ajoutee aux taxes du dossier (Configuration > General)",
    ]


@pytest.mark.functionality
def test_a_rate_with_its_listed_tax_changes_nothing(experio):
    experio["taxes"].append({"id": 4960, "rate": 10.0, "account_id": [243204, "34550000 État"]})
    experio["listed"].append(4960)
    assert taxes.add_missing_taxes(272379, {}) == [] and experio["created"] == []


@pytest.mark.functionality
def test_in_test_mode_nothing_is_created(experio):
    experio["dry"] = True
    assert taxes.add_missing_taxes(272379, {}) == ["taxe TVA 10% ACHATS serait créée (compte 34550000)"]
    assert experio["created"] == []


@pytest.mark.functionality
def test_the_dgi_code_follows_the_cabinets_table(experio):
    assert taxes.dgi_code("purchase", 20, False, "61110000") == 74  # 146, achat a l'interieur
    assert taxes.dgi_code("purchase", 10, True, "61110000") is None  # 149: not in this fake Experio
    assert taxes.DGI_CODES[("purchase", 10, True)] == 149
    assert taxes.dgi_code("purchase", 14, False, "61110000") is None  # not in the table: no code
