"""Finding the dossier the client means from the name they give (fictional dossiers)."""

import pytest

from engine.tools.core.config import MIN_SIMILARITY
from engine.tools.core.words import similarity

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
    from engine.tools.dossiers import search

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
    from engine.tools.core.app import ToolError
    from engine.tools.dossiers import search

    monkeypatch.setattr(search, "dossiers_or_none", lambda step: None)
    with pytest.raises(ToolError, match="liste des dossiers"):
        search.resolve_dossier("Atlas")


@pytest.mark.communication
def test_the_only_close_dossier_is_taken_and_the_client_told_which(resolve):
    result = resolve("benali")  # only Cabinet Benali is close
    assert result["status"] == "ok" and result["dossier_id"] == 5 and not result.get("routine")
    assert "Cabinet Benali" in result["message"] and "« benali »" in result["message"]


@pytest.mark.communication
def test_dossiers_are_counted_by_state_their_names_only_when_asked(monkeypatch):
    from engine.tools.dossiers import search

    groups = [{"state": "active", "state_count": 339}, {"state": "archived", "state_count": 11}]
    monkeypatch.setattr(search, "odoo_call", lambda *args, **kw: groups)
    monkeypatch.setattr(search, "dossiers_or_none", lambda step: [{"id": 1, "name": "Cabinet Benali"}])
    assert search.list_dossiers() == "350 dossiers : 339 actifs, 0 nouveaux, 11 archives."
    assert "Cabinet Benali" in search.list_dossiers(names=True)
