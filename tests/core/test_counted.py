from engine.tools.core.words import counted, reason, reasons


def test_counted_agrees_with_its_count():
    assert counted(0, "{} ligne lue", "{} lignes lues") == "0 ligne lue"
    assert counted(1, "{} opération prête", "{} opérations prêtes") == "1 opération prête"
    assert counted(3, "{} opération prête", "{} opérations prêtes") == "3 opérations prêtes"


def test_reason_lowercases_a_word_not_an_acronym():
    assert reason("Taux absent.") == "taux absent"
    assert reason("IF manquant") == "IF manquant"
    assert reasons({"Taux absent.": 2, "TVA manquante": 1}, "{} ligne", "{} lignes") == [
        "2 lignes : taux absent.",
        "1 ligne : TVA manquante.",
    ]


def test_a_reason_starting_with_an_apostrophe_is_lowercased_an_acronym_kept():
    assert reason("L'opération doit être définie.") == "l'opération doit être définie"
    assert reason("IF manquant") == "IF manquant"
