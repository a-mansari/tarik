"""The side of the client's invoices: the agent reads it and quotes the client's words; the code only checks that the
quote is the client's, so no list of words says what « achat » looks like."""

import pytest

from engine.tools.core.conversation import remember_client_message
from engine.tools.invoices.classify import verified_side


@pytest.mark.security
@pytest.mark.parametrize(
    ("invoice_type", "quote", "kept"),
    [
        ("achat", "factures d’achat", "achat"),  # the quote as the client typed it, curly apostrophe included
        ("achat", "Factures d'achat", "achat"),  # case and apostrophe aside
        ("achat", "règlement fournisseur", "achat"),  # no word list: the agent recognised it, the quote is theirs
        ("vente", "factures d’achat", "vente"),  # the quote is theirs; the agent's reading of it is its own
        ("mixte", "achats et ventes", ""),  # not in the messages
        ("achat", "mes achats du mois", ""),  # words the client never wrote
        ("achat", "", ""),  # no quote: nothing shown
        ("achat", "de", ""),  # too short to prove anything
        ("autre", "factures d’achat", ""),  # not a side
        ("", "factures d’achat", ""),
    ],
)
def test_a_side_stands_only_with_words_found_in_the_clients_messages(db_cabinet, invoice_type, quote, kept):
    remember_client_message("s-side", "règlement fournisseur : voici les factures d’achat de ZZ TEST 2025")
    found = verified_side("s-side", invoice_type, quote)
    assert found["status"] == "ok" and found["invoice_type"] == kept


@pytest.mark.security
def test_the_words_may_come_from_an_earlier_message_of_the_conversation(db_cabinet):
    remember_client_message("s-earlier", "ce sont des achats")
    remember_client_message("s-earlier", "c'est pour le dossier ZZ TEST")
    assert verified_side("s-earlier", "achat", "ce sont des achats")["invoice_type"] == "achat"
