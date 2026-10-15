"""Non-recoverable TVA: the client's oui / non."""

import pytest

from tarik.invoices.tva import vat_answer


@pytest.mark.functionality
def test_the_answer_is_read():
    assert vat_answer("Oui") is True and vat_answer("non recuperable") is True
    assert vat_answer("non") is False and vat_answer("recuperable") is False
    assert vat_answer("je ne sais pas") is None and vat_answer("") is None
