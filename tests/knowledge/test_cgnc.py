"""The CGNC Tarik looks up: its files as split_cgnc.py wrote them, loaded once per version, found by account code
(a dossier's own sub-account by the accounts it starts with) and by words.

Loaded in the test database's schema knowledge, the same for every run, as on a stack: two runs at once with
different files load in turn, and a test never asks which version it reads."""

import pytest
from psycopg.conninfo import make_conninfo

from engine.knowledge import cgnc
from engine.tools.core import cabinets
from tests import testdb


def conninfo():
    return make_conninfo(**{**cabinets.POSTGRES, "dbname": testdb.DATABASE})


@pytest.fixture(scope="module")
def db(_test_database):
    """The CGNC loaded in the test database."""
    cgnc.load(conninfo())
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
    # a label the PDF wraps is whole: none ends on a word that wants another, nor an open bracket
    cut = [a["code"] for a in accounts if a["label"].split()[-1] in ("de", "des", "du", "et", "aux", "-")]
    assert cut == [] and all(a["label"].count("(") == a["label"].count(")") for a in accounts)
    assert next(a for a in accounts if a["code"] == "3455")["label"] == "Etat - TVA récupérable"  # not « T V A . »


@pytest.mark.technical
def test_loaded_once_per_version(db):
    cgnc.load(conninfo())  # this version, whatever another run loaded meanwhile
    assert cgnc.load(conninfo()) is False
    assert db.execute("select count(*) from knowledge.cgnc_pieces").fetchone()[0] == len(cgnc.rows("pieces.jsonl"))


@pytest.mark.functionality
def test_an_account_finds_its_rule_and_the_accounts_it_names(db):
    found = cgnc.search(db, code="6125")
    assert [account["code"] for account in found["account"]] == ["61", "612", "6125"]
    rule = next(piece for piece in found["pieces"] if piece["place"].startswith("TITRE IV"))
    assert "3491" in rule["body"] and "612." in rule["place"]
    assert "3491" in [account["code"] for account in found["linked"]]


@pytest.mark.functionality
def test_a_rubric_finds_its_own_pieces(db):
    places = [piece["place"] for piece in cgnc.search(db, code="61")["pieces"]]
    assert places and "CLASSE 6" in places[0].upper()


@pytest.mark.functionality
def test_the_two_plans_are_told_apart(db):
    places = [piece["place"] for piece in cgnc.search(db, code="2310")["pieces"]]
    assert any("Modèle normal" in place and "231 Terrains" in place for place in places)
    assert any("Modèle simplifié" in place for place in places)


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


@pytest.mark.functionality
def test_words_rank_the_pieces_of_a_code(db):
    found = cgnc.search(db, code="6125", words="charges constatées d'avance")
    assert "constatées d’avance" in found["pieces"][0]["body"] or "constatées d'avance" in found["pieces"][0]["body"]


@pytest.mark.functionality
def test_tva_is_found_however_the_pdf_spaced_it(db):
    assert any("345" in piece["place"] for piece in cgnc.search(db, words="TVA récupérable")["pieces"])
