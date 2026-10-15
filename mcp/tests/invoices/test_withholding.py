"""Withholding answers (tarik-master's parsers) and how they read back."""

import pytest

from tarik.invoices.withholding import described, local_withholding_answer, withholding_answer


@pytest.mark.functionality
def test_foreign_withholding():
    assert withholding_answer("2") == {"retenue": True, "foreign_withholding": "supplier", "local_service": True}
    assert withholding_answer("a la charge de la société, hors Maroc") == {"retenue": True, "foreign_withholding": "society", "local_service": False}
    assert withholding_answer("sans")["retenue"] is False and withholding_answer("je ne sais pas") is None
    assert described(withholding_answer("2, hors maroc")) == "retenue etrangere a la charge du fournisseur, service rendu hors Maroc"


@pytest.mark.functionality
def test_local_withholding():
    assert local_withholding_answer("10 et 75") == {"retenue": True, "local_withholding_percentage": "10", "tax_withholding_percentage": "75"}
    assert local_withholding_answer("sans")["retenue"] is False and local_withholding_answer("peut-etre") is None
    assert described(local_withholding_answer("10 et 75")) == "retenue a la source IS/IR 10 % et TVA 75 %"
