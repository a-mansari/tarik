"""Exercice choice, read in code (tarik-master asked a model)."""

import pytest

from tarik.invoices.exercice import exercice_choice

OPENS = [{"id": 1, "name": "Exercice 2024", "date_start": "2024-01-01", "date_end": "2024-12-31"},
         {"id": 2, "name": "Exercice 2025", "date_start": "2025-01-01", "date_end": "2025-12-31"}]


@pytest.mark.functionality
def test_the_choice_is_read():
    assert exercice_choice("dans l'exercice 2025 svp", OPENS) == ("old", OPENS[1])
    assert exercice_choice("Exercice 2024", OPENS) == ("old", OPENS[0])
    assert exercice_choice("cree un nouvel exercice", OPENS) == ("new", None) and exercice_choice("nouveau", OPENS) == ("new", None)
    assert exercice_choice("le premier", OPENS) is None and exercice_choice("2023", OPENS) is None  # unclear, or not an open one
