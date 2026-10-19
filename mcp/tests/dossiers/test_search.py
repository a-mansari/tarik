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
