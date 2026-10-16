"""Withholding answers: read by the model, checked here against what Experio takes, and how they read back."""

import pytest

from tarik.invoices import withholding
from tarik.invoices.withholding import described, local_withholding_answer, withholding_answer


@pytest.fixture
def model(monkeypatch):
    """The model's reading, set by each test; the client's own answer reaches it word for word."""
    reading, seen = {}, []
    monkeypatch.setattr(withholding, "ask_model", lambda system, pages, instruction, what: seen.append(instruction) or dict(reading))
    return reading, seen


@pytest.mark.functionality
def test_foreign_withholding(model):
    reading, seen = model
    reading.update(rule="society", abroad=True)
    assert withholding_answer("a la charge de la societe, contrat 2025, hors Maroc") == \
        {"retenue": True, "foreign_withholding": "society", "local_service": False}
    assert "a la charge de la societe, contrat 2025, hors Maroc" in seen[0]
    reading.update(rule="none", abroad=False)
    assert withholding_answer("sans")["retenue"] is False
    reading.update(rule="maybe")
    assert withholding_answer("je ne sais pas") is None  # nothing Experio takes: asked again
    reading.update(rule="supplier", abroad=True)
    assert described(withholding_answer("2, hors maroc")) == "retenue etrangere a la charge du fournisseur, service rendu hors Maroc"


@pytest.mark.functionality
def test_local_withholding(model):
    reading, _ = model
    reading.update(retenue=True, is_ir=10, tva=75)
    assert local_withholding_answer("10 et 75") == {"retenue": True, "local_withholding_percentage": "10", "tax_withholding_percentage": "75"}
    assert described(local_withholding_answer("10 et 75")) == "retenue a la source IS/IR 10 % et TVA 75 %"
    reading.update(retenue=True, is_ir="10%", tva="75 %")
    assert local_withholding_answer("10 % et 75 %")["local_withholding_percentage"] == "10"
    assert local_withholding_answer("10 % et 75 %")["tax_withholding_percentage"] == "75"
    reading.update(retenue=False)
    assert local_withholding_answer("sans, facture du 15/03")["retenue"] is False


@pytest.mark.security
@pytest.mark.parametrize("reading", [{"retenue": True, "is_ir": 15.5, "tva": 0}, {"retenue": True, "is_ir": 12, "tva": 0}, {"retenue": True, "is_ir": "dix", "tva": 75},
                                     {"retenue": True, "is_ir": 10, "tva": 50}, {"retenue": True, "is_ir": None, "tva": 0},
                                     {"retenue": None, "is_ir": 10, "tva": 0}, {"retenue": "oui", "is_ir": 10}])
def test_a_rate_experio_does_not_take_is_never_written(model, reading):
    model[0].update(reading)
    assert local_withholding_answer("x") is None
