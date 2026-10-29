from engine.tools.core.words import counted


def test_counted_agrees_with_its_count():
    assert counted(0, "{} ligne lue", "{} lignes lues") == "0 ligne lue"
    assert counted(1, "{} opération prête", "{} opérations prêtes") == "1 opération prête"
    assert counted(3, "{} opération prête", "{} opérations prêtes") == "3 opérations prêtes"
