"""Prepaid: the period rule."""

import pytest

from engine.tools.invoices.prepaid import prepaid_period


@pytest.mark.functionality
def test_the_billed_period():
    assert prepaid_period("2026-01-01", "2026-12-31") == {
        "prepaid_start_date": "2026-01-01",
        "prepaid_end_date": "2026-12-31",
        "prepaid_months": 12,
    }
    assert prepaid_period("2026-09-01", "2026-09-30") is None and prepaid_period(None, "2026-12-31") is None
    assert prepaid_period("2026-12-31", "2026-01-01") is None
