"""Words as the code compares them: what a client's email client types must read like plain ASCII."""

import pytest

from engine.tools.core.words import normalized, said
from engine.tools.invoices.classify import client_side


@pytest.mark.functionality
def test_normalized_drops_accents_and_ligatures():
    assert normalized("Sté  Alliance-Génie") == "ste alliance genie"
    assert normalized("Cœur de l'été Æsir") == "coeur de l ete aesir"


@pytest.mark.functionality
@pytest.mark.parametrize("mark", ["'", "’", "‘", "ʼ", "´", "`"])
def test_every_apostrophe_separates_words(mark):
    assert normalized(f"factures d{mark}achat") == "factures d achat"
    assert said("achat", normalized(f"factures d{mark}achat"))


@pytest.mark.functionality
def test_other_typography_separates_words_too():
    assert normalized("« oui » – non… peut‑être") == "oui non peut etre"
    assert normalized("a b") == "a b"  # a no-break space


@pytest.mark.functionality
def test_an_email_clients_curly_apostrophe_still_says_the_side():
    assert client_side("achat", "factures d’achat de ZZ TEST LED 2025, tu peux les traiter") == "achat"
