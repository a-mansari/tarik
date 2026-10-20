"""The client's words -> one account (fictional chart, same shapes as a real one)."""

import pytest

from tarik.invoices import accounts
from tarik.invoices.accounts import account_meant, resolve_account

CHART = [
    {"id": 1, "code": "61110000", "name": "Achats de marchandises"},
    {"id": 2, "code": "61120000", "name": "Achats de marchandises"},
    {"id": 3, "code": "61190000", "name": "Rabais, remises et ristournes obtenus sur achats de marchandises"},
    {"id": 4, "code": "61251000", "name": "Achats de fournitures non stockables"},
    {"id": 5, "code": "61455000", "name": "Frais de téléphone"},
]


@pytest.mark.functionality
def test_one_account_or_a_question():
    assert resolve_account(CHART, "achats de marchandises")["id"] == 1  # same label twice: lowest code
    assert resolve_account(CHART, "6111")["id"] == 1 and resolve_account(CHART, "61455000")["id"] == 5
    assert resolve_account(CHART, "téléphone")["id"] == 5
    assert resolve_account(CHART, "marchandises") is None  # three different labels: ask
    assert resolve_account(CHART, "611") is None and resolve_account(CHART, "carburant") is None
    tel = [
        {"id": 6, "code": "61450000", "name": "Frais postaux et telecommunications"},
        {"id": 7, "code": "61451000", "name": "Frais postaux"},
        {"id": 8, "code": "61455000", "name": "Frais de telephone"},
    ]
    assert (
        resolve_account(tel, "6145")["id"] == 6 and resolve_account(tel, "61455")["id"] == 8
    )  # a short code: padded with zeros


@pytest.mark.security
@pytest.mark.parametrize(
    "read, expected",
    [("61455000", 5), (61455000, 5), ("6145", 5), ("611", None), ("99999999", None), ("frais", None), (None, None)],
)
def test_a_sentence_is_read_by_the_model_but_only_the_charts_accounts_count(monkeypatch, read, expected):
    monkeypatch.setattr(accounts, "ask_model", lambda *args: {"code": read})
    found = account_meant(CHART, "Pour MEDI TELECOM, utilisez le compte 6145.")
    assert (found or {}).get("id") == expected


@pytest.mark.functionality
def test_a_code_start_the_client_wrote_is_theirs_not_the_models(monkeypatch):
    travel = [
        {"id": 9, "code": "61431000", "name": "Voyages et déplacements"},
        {"id": 10, "code": "61436000", "name": "Réceptions"},
    ]
    monkeypatch.setattr(accounts, "ask_model", lambda *args: {"code": "61431000"})
    assert account_meant(travel, "6143") is None  # two accounts start with it: asked again
    assert account_meant(travel, "Mettez 6143, 2 factures") is None
    assert account_meant(travel, "61431 voyages")["id"] == 9
    assert account_meant(travel, "Voyages et déplacements")["id"] == 9  # no code written: the model's
