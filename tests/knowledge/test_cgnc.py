"""The CGNC Tarik looks up: its files as split_cgnc.py wrote them, loaded once per version, found by account code
(a dossier's own sub-account by the accounts it starts with) and by words."""

import pytest
from psycopg.conninfo import make_conninfo

from engine.knowledge import cgnc
from engine.tools.core import cabinets
from tests import testdb


@pytest.fixture(scope="module")
def db(_test_database):
    """The CGNC loaded in the test database (schema orchestrator there: the same for every run, as on a stack)."""
    cgnc.load(make_conninfo(**{**cabinets.POSTGRES, "dbname": testdb.DATABASE}))
    with testdb.connect() as connection:
        yield connection


@pytest.mark.technical
def test_the_files_are_whole():
    pieces, accounts = cgnc.rows("pieces.jsonl"), cgnc.rows("accounts.jsonl")
    assert [piece["id"] for piece in pieces] == list(range(1, len(pieces) + 1))
    assert all(piece["body"].strip() and piece["page_start"] <= piece["page_end"] for piece in pieces)
    codes = {account["code"] for account in accounts}
    assert len(codes) == len(accounts)
    assert all(account["parent"] in codes for account in accounts if account["parent"])


@pytest.mark.technical
def test_loaded_once_per_version(db):
    conninfo = make_conninfo(**{**cabinets.POSTGRES, "dbname": testdb.DATABASE})
    assert cgnc.load(conninfo) is False  # the fixture loaded this version
    assert db.execute("select count(*) from orchestrator.cgnc_pieces").fetchone()[0] == len(cgnc.rows("pieces.jsonl"))


@pytest.mark.functionality
def test_an_account_finds_its_rule_and_the_accounts_it_names(db):
    found = cgnc.search(db, code="6125")
    assert [account["code"] for account in found["account"]] == ["61", "612", "6125"]
    rule = next(piece for piece in found["pieces"] if piece["place"].startswith("TITRE IV"))
    assert "3491" in rule["body"] and "612." in rule["place"]
    assert "3491" in [account["code"] for account in found["linked"]]


@pytest.mark.functionality
def test_a_dossiers_own_sub_account_is_found_by_the_accounts_it_starts_with(db):
    found = cgnc.search(db, code="61251001")
    assert found["account"][-1]["code"] == "61251"
    assert found["pieces"]


@pytest.mark.functionality
def test_words_find_their_piece_accents_aside(db):
    found = cgnc.search(db, words="charges constatees d'avance")
    assert "CHARGES CONSTATEES D'AVANCE" in found["pieces"][0]["place"]  # the terminology's
    assert any("349" in piece["place"] for piece in found["pieces"])  # and its account
    assert cgnc.search(db, words="")["pieces"] == []
