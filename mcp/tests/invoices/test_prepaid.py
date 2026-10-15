"""Prepaid: the period rule (tarik-master) and the yes/no read in code."""

import pytest

from tarik.core.conversation import yes_no
from tarik.invoices.prepaid import prepaid_period


@pytest.mark.functionality
def test_the_billed_period():
    assert prepaid_period("2026-01-01", "2026-12-31") == {"prepaid_start_date": "2026-01-01", "prepaid_end_date": "2026-12-31", "prepaid_months": 12}
    assert prepaid_period("2026-09-01", "2026-09-30") is None and prepaid_period(None, "2026-12-31") is None
    assert prepaid_period("2026-12-31", "2026-01-01") is None


@pytest.mark.functionality
def test_yes_or_no():
    assert yes_no("Oui, vas-y") is True and yes_no("répartis-la") is True and yes_no("ok") is True
    assert yes_no("Non, c'est ponctuel") is False and yes_no("pas besoin") is False and yes_no("ne pas repartir") is False
    assert yes_no("je ne sais pas") is None and yes_no("") is None
