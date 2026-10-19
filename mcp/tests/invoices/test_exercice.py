"""Exercice choice, read in code, never by a model."""

import pytest

from tarik.invoices.exercice import exercice_choice, given_date

OPENS = [
    {"id": 1, "name": "Exercice 2024", "date_start": "2024-01-01", "date_end": "2024-12-31"},
    {"id": 2, "name": "Exercice 2025", "date_start": "2025-01-01", "date_end": "2025-12-31"},
]


@pytest.mark.functionality
def test_the_agents_choice_is_taken_only_among_the_open_exercices():
    assert exercice_choice("Exercice 2025", OPENS) == ("old", OPENS[1])  # named as the question lists it
    assert exercice_choice(str(OPENS[0]["id"]), OPENS) == ("old", OPENS[0])
    assert exercice_choice("nouveau", OPENS) == exercice_choice(" Nouveau ", OPENS) == ("new", None)
    for unclear in ("", "Exercice 2023", "2025", "cree un nouvel exercice", "le premier"):
        assert exercice_choice(unclear, OPENS) is None, unclear  # the client is asked again


@pytest.mark.functionality
def test_the_date_given_is_one_full_date():
    assert given_date("du 15/03/2025") == "2025-03-15" and given_date("2025-03-15") == "2025-03-15"
    assert given_date("ref 12-3-45 le 15/03/2025") == "2025-03-15"  # a reference is no date
    assert (
        given_date("le 15/03/25") is None and given_date("15/03/2025 ou 16/03/2025") is None and given_date("") is None
    )


@pytest.mark.functionality
def test_an_exercice_covering_the_date_is_linked_whatever_the_answer(monkeypatch):
    """The cabinet opened the year by hand between the question and the answer: the invoice goes there."""
    from tarik.invoices import exercice

    invoice = {
        "state": "draft",
        "company_id": [5, "D"],
        "exercice_id": False,
        "invoice_date": "2025-03-15",
        "is_old_invoice": False,
    }
    monkeypatch.setattr(exercice, "require_draft_invoice", lambda invoice_id, fields, what: dict(invoice))
    monkeypatch.setattr(exercice, "odoo_call", lambda *args, **kw: OPENS)
    monkeypatch.setattr(
        exercice, "relink", lambda invoice_id, invoice, found: {"status": "ok", "message": found["name"]}
    )
    assert exercice.settle_exercice(3, answer="nouveau", decision="nouveau")["message"] == "Exercice 2025"
