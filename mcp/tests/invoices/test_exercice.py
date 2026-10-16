"""Exercice choice, read in code (tarik-master asked a model)."""

import pytest

from tarik.invoices.exercice import exercice_choice, given_date

OPENS = [{"id": 1, "name": "Exercice 2024", "date_start": "2024-01-01", "date_end": "2024-12-31"},
         {"id": 2, "name": "Exercice 2025", "date_start": "2025-01-01", "date_end": "2025-12-31"}]


@pytest.mark.functionality
def test_the_choice_is_read():
    assert exercice_choice("dans l'exercice 2025 svp", OPENS) == ("old", OPENS[1])
    assert exercice_choice("Exercice 2024", OPENS) == ("old", OPENS[0])
    assert exercice_choice("cree un nouvel exercice", OPENS) == ("new", None) and exercice_choice("nouveau", OPENS) == ("new", None)
    assert exercice_choice("le premier", OPENS) is None and exercice_choice("2023", OPENS) is None  # unclear, or not an open one


@pytest.mark.functionality
def test_a_new_exercice_only_when_that_is_all_the_client_says():
    opens = [{"id": 1, "name": "Exercice 2024", "date_start": "2024-01-01", "date_end": "2024-12-31"}]
    assert exercice_choice("nouveau", opens) == ("new", None)
    assert exercice_choice("ne creez pas de nouvel exercice, mettez-la dans 2024", opens) is None
    assert exercice_choice("dans 2024", opens) == ("old", opens[0])


@pytest.mark.functionality
def test_the_date_given_is_one_full_date():
    assert given_date("du 15/03/2025") == "2025-03-15" and given_date("2025-03-15") == "2025-03-15"
    assert given_date("ref 12-3-45 le 15/03/2025") == "2025-03-15"  # a reference is no date
    assert given_date("le 15/03/25") is None and given_date("15/03/2025 ou 16/03/2025") is None and given_date("") is None
