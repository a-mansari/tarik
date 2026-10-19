"""The dossier's settings before posting: Experio's own tax creation and auto-detection, then what still blocks."""

import pytest

from tarik.invoices import configuration


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: what its check says before and after its own detection; every call made."""
    state = {
        "before": [("cgnc", "<p>The tax with the 20 rate does not exist</p>")],
        "after": [],
        "calls": [],
        "taxes": 2,
    }
    monkeypatch.setattr(
        configuration, "require_invoice", lambda i, f: {"state": "draft", "company_id": [5, "D"], "exercice_id": False}
    )
    monkeypatch.setattr(configuration, "dry_run", lambda: False)

    def odoo_json(model, method, ids, **kw):
        state["calls"].append((model, method))
        if method == "collect_validation_messages":
            return state["after"] if "auto_detect_settings" in [c[1] for c in state["calls"]] else state["before"]
        if method == "create_tax_ids":
            state["taxes"] += 3

    def odoo_call(model, method, *a, **k):
        state["calls"].append((model, method))
        return state["taxes"] if method == "search_count" else [31, 32]

    monkeypatch.setattr(configuration, "odoo_json", odoo_json)
    monkeypatch.setattr(configuration, "odoo_call", odoo_call)
    return state


@pytest.mark.functionality
def test_experios_own_detection_runs_only_when_its_check_blocks(experio):
    result = configuration.configure_cabinet(3)
    assert [c[1] for c in experio["calls"]].count("create_tax_ids") == 1
    assert ("res.company", "auto_detect_settings") in experio["calls"]
    assert result["message"] == (
        "Facture 3 : detection automatique des parametres du dossier lancee, 3 taxe(s) creee(s) : plus rien ne bloque "
        "la comptabilisation."
    )
    experio["before"], experio["calls"] = [], []
    assert configuration.configure_cabinet(3)["routine"] and len(experio["calls"]) == 1  # nothing missing: one read


@pytest.mark.communication
def test_what_still_blocks_is_named_for_the_cabinet(experio):
    experio["after"] = [
        ("cgnc", "<p>The configuration of <b>local supplier journal</b> is not defined.</p>"),
        ("warning", "x"),
    ]
    result = configuration.configure_cabinet(3)
    assert result["message"].endswith(
        "Reste a parametrer par le cabinet : The configuration of local supplier journal is not defined."
    )
