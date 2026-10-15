"""Purchase or sale: only as the client wrote it, never the model's guess."""

import pytest

from tarik.invoices.classify import client_side


@pytest.mark.security
@pytest.mark.parametrize("side, words", [
    ("achat", "Voici mes factures d'achat pour GECAFISC"),
    ("achat", "les factures fournisseurs de mars"),
    ("vente", "Saisissez ces ventes"),
    ("vente", "Voici les factures clients du dossier"),
    ("mixte", "des achats et des ventes"),
    ("achat", "ce ne sont pas des ventes mais des achats"),
])
def test_the_side_the_client_wrote_is_kept(side, words):
    assert client_side(side, words) == side


@pytest.mark.security
@pytest.mark.parametrize("side, words", [
    ("achat", "Voici mes factures pour GECAFISC"),  # not said: the client is asked
    ("vente", "Voici les factures d'achat"),  # not what they said
    ("mixte", "factures d'achat"),  # mixte needs both
    ("achat", "mon client m'a envoye ses factures"),  # "client" alone says nothing
    ("purchase", "factures d'achat"),  # not a side
    ("", "factures d'achat"),
])
def test_a_side_the_client_did_not_write_is_refused(side, words):
    assert client_side(side, words) is None
