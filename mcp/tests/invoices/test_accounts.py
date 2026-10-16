"""The client's words -> one account (fictional chart, same shapes as a real one)."""

import pytest

from tarik.invoices import accounts
from tarik.invoices.accounts import account_meant, resolve_account

CHART = [{"id": 1, "code": "61110000", "name": "Achats de marchandises"}, {"id": 2, "code": "61120000", "name": "Achats de marchandises"},
         {"id": 3, "code": "61190000", "name": "Rabais, remises et ristournes obtenus sur achats de marchandises"},
         {"id": 4, "code": "61251000", "name": "Achats de fournitures non stockables"}, {"id": 5, "code": "61455000", "name": "Frais de téléphone"}]


@pytest.mark.functionality
def test_one_account_or_a_question():
    assert resolve_account(CHART, "achats de marchandises")["id"] == 1  # same label twice: lowest code
    assert resolve_account(CHART, "6111")["id"] == 1 and resolve_account(CHART, "61455000")["id"] == 5
    assert resolve_account(CHART, "téléphone")["id"] == 5
    assert resolve_account(CHART, "marchandises") is None  # three different labels: ask
    assert resolve_account(CHART, "611") is None and resolve_account(CHART, "carburant") is None


@pytest.mark.security
@pytest.mark.parametrize("read, expected", [("61455000", 5), ("6145", None), ("99999999", None), (None, None)])
def test_a_sentence_is_read_by_the_model_but_only_the_charts_accounts_count(monkeypatch, read, expected):
    monkeypatch.setattr(accounts, "ask_model", lambda *args: {"code": read})
    found = account_meant(CHART, "Pour MEDI TELECOM, utilisez le compte 6145.")
    assert (found or {}).get("id") == expected
