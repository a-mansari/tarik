"""Finding the dossier the client means from the name they give (fictional dossiers)."""

import pytest

from tarik.core.config import MIN_SIMILARITY
from tarik.core.words import similarity

DOSSIERS = [
    "STE ALLIANCE GENIE CIVIL SARL",
    "Alliance Transport",
    "Boulangerie Atlas",
    "ATLAS IMMOBILIER",
    "Cabinet Benali",
]


def best(query):
    return max(DOSSIERS, key=lambda name: similarity(query, name))


@pytest.mark.functionality
def test_the_closest_dossier_is_found():
    assert best("alliance genie civil") == "STE ALLIANCE GENIE CIVIL SARL"
    assert best("Alliance Génie-Civil") == "STE ALLIANCE GENIE CIVIL SARL"  # accents, hyphen
    assert best("boulangeri atlas") == "Boulangerie Atlas"  # typo
    assert best("immobilier atlas") == "ATLAS IMMOBILIER"  # word order
    assert best("benali") == "Cabinet Benali"  # part of the name
    assert similarity("benali", "Cabinet Benali") >= 0.9


@pytest.mark.functionality
def test_unrelated_names_stay_below_the_threshold():
    assert max(similarity("xyz", name) for name in DOSSIERS) < MIN_SIMILARITY
    assert similarity("zzqx boulangerie inexistante", "FinStrat Consulting") < MIN_SIMILARITY
    assert similarity("", "Atlas") == 0.0


@pytest.fixture
def resolve(monkeypatch):
    """resolve_dossier over the fictional DOSSIERS, with Experio's listing replaced."""
    from tarik.dossiers import search

    monkeypatch.setattr(
        search, "dossiers_or_none", lambda step: [{"id": i, "name": n} for i, n in enumerate(DOSSIERS, 1)]
    )
    return search.resolve_dossier


@pytest.mark.functionality
def test_a_name_that_is_one_dossier_gives_its_id(resolve):
    result = resolve("cabinet benali")
    assert result["status"] == "ok" and result["dossier_id"] == 5 and result["routine"]
    assert resolve("  Alliance Transport ")["dossier_id"] == 2  # spaces and case do not matter


@pytest.mark.communication
def test_a_name_that_is_not_one_dossier_asks_the_client_and_gives_no_id(resolve):
    nothing = resolve("xyz")
    assert nothing["status"] == "stop" and "dossier_id" not in nothing and "Aucun dossier" in nothing["message"]
    assert resolve("")["status"] == "stop" and resolve("   ")["status"] == "stop"
    several = resolve("atlas")  # two dossiers carry it
    assert several["status"] == "stop" and "dossier_id" not in several
    assert "Boulangerie Atlas" in several["message"] and "ATLAS IMMOBILIER" in several["message"]
    assert "(id" not in several["message"]  # the client never reads an id


@pytest.mark.technical
def test_experio_not_giving_the_dossiers_is_an_error_not_a_guess(monkeypatch):
    from tarik.core.app import ToolError
    from tarik.dossiers import search

    monkeypatch.setattr(search, "dossiers_or_none", lambda step: None)
    with pytest.raises(ToolError, match="liste des dossiers"):
        search.resolve_dossier("Atlas")
