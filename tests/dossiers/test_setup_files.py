"""A dossier set up from separate files: what each file is, the bank a client names, the bank of each bank
journal asked and set, and an onboarding that waits for the files until the client says all were sent."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
from engine.tools.dossiers import setup
from engine.tools.dossiers.setup import bank_named, file_kind

BANKS = [
    {"id": 3, "name": "CREDIT IMMOBILIER ET HOTELIER (CIH)"},
    {"id": 4, "name": "ATTIJARIWAFA BANK"},
    {"id": 5, "name": "BANQUE CENTRALE POPULAIRE (BP)"},
    {"id": 6, "name": "SAHAM BANK"},
    {"id": 7, "name": "BANK OF AFRICA (BMCE)"},
]


@pytest.mark.functionality
def test_what_a_file_is_from_the_clients_word_else_its_headers():
    assert file_kind("le plan comptable", "x") == "plan" and file_kind("Journaux", "x") == "journaux"
    assert file_kind("mes fournisseurs", "x") == "fournisseurs" and file_kind("grand livre", "x") == "grand livre"
    assert file_kind("", "JournalCode;EcritureDate;CompteNum;Debit;Credit\nAC;20250101;6111;10;0") == "fec"
    assert file_kind("", "Code;Intitulé;Type de compte\n6111;Achats;charge") == "plan"
    assert file_kind("", "Code journal;Nom du journal;Type;Banque\nBQ;Banque;bank;CIH") == "journaux"
    assert file_kind("", "Nom;ICE;Compte tiers fournisseur\nATLAS;001;44110001") == "fournisseurs"
    assert file_kind("", "Nom;ICE;Compte tiers client\nNOVA;002;34210001") == "clients"
    assert file_kind("", "Colonne A;Colonne B\n1;2") is None  # nothing tells: the client is asked


@pytest.mark.functionality
def test_the_bank_a_client_names():
    assert bank_named("CIH", BANKS)["id"] == 3 and bank_named("le compte attijari", BANKS)["id"] == 4
    assert bank_named("Banque Populaire", BANKS)["id"] == 5 and bank_named("BP", BANKS)["id"] == 5
    assert bank_named("societe generale", BANKS)["id"] == 6 and bank_named("BMCE", BANKS)["id"] == 7
    assert bank_named("la banque", BANKS) is None and bank_named("Chaabi", BANKS) is None


@pytest.fixture
def experio(monkeypatch):
    """Dossier 557 with two bank journals without a bank; what was written."""
    state = {"journals": [{"id": 11, "name": "BQ1"}, {"id": 12, "name": "BQ2"}], "written": []}
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "dry_run", lambda: False)
    monkeypatch.setattr(setup, "test_mode_note", lambda: "")

    def odoo_call(model, method, *args, **kw):
        if model == "exp.journal" and method == "search_read":
            return list(state["journals"])
        if model == "res.bank" and method == "search_read":
            return BANKS
        if model == "res.bank":
            return [bank for bank in BANKS if bank["id"] == args[0][0]]
        if model == "exp.journal" and method == "read":
            return [{"name": "BQ1", "company_id": [557, "Gecafisc"], "partner_id": False}]
        if model == "exp.journal" and method == "write":
            state["written"].append(args[1])
            state["journals"].pop(0)
            return True
        if model == "exp.partner":
            return [{"id": 99}]
        raise AssertionError((model, method))

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    return state


@pytest.mark.communication
def test_each_bank_journal_is_asked_its_bank_in_turn(experio):
    asked = setup.ask_banks(557)
    assert asked["status"] == "question" and asked["journal_id"] == 11 and asked["choices"][3].startswith("CREDIT")
    assert "journal `BQ1` est a quelle banque" in asked["message"] and "1 autre(s)" in asked["message"]
    following = setup.set_journal_bank(557, 11, 3)
    assert experio["written"] == [{"bank_id": 3, "partner_id": 99}]  # the bank, and the tiers named after it
    assert following["status"] == "question" and following["journal_id"] == 12  # the next one, in the same reply
    assert following["message"].startswith("Dossier `Gecafisc` : journal `BQ1` a la banque `CREDIT")
    experio["journals"].clear()
    assert setup.ask_banks(557)["routine"]


@pytest.mark.communication
def test_a_bank_the_journals_name_says_in_full_is_set_and_said_not_asked(experio):
    experio["journals"][0]["name"] = "Banque ATTIJARIWAFA BANK"
    result = setup.ask_banks(557)
    assert experio["written"] == [{"bank_id": 4, "partner_id": 99}]
    assert "a la banque `ATTIJARIWAFA BANK` (d'après son nom" in result["message"]
    assert result["status"] == "question" and result["journal_id"] == 12  # BQ2 names none: asked
    assert not setup.named_in_full("Banque Populaire", BANKS[2])  # a part of its name only: asked
    assert not setup.named_in_full("BQ CREDIT", BANKS[0])


@pytest.mark.functionality
def test_without_entries_the_onboarding_waits_for_the_clients_files(monkeypatch):
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "in_dossier", lambda dossier_id: {})
    monkeypatch.setattr(setup, "setup_waits", lambda dossier_id, file_id=None: None)
    monkeypatch.setattr(setup, "exercice_note", lambda dossier_id, dossier: None)
    monkeypatch.setattr(setup, "dry_run", lambda: True)
    counts = {"experio.history": 0, "exp.account": 12, "exp.journal": 0, "exp.partner": 0}

    def odoo_call(model, method, *args, **kw):
        return [{"show_onboarding": True}] if method == "read" else counts[model]

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    waiting = setup.finish_setup(557)
    assert "Il manque : journaux, fournisseurs, clients." in waiting["message"]
    assert "integration terminee" in setup.finish_setup(557, finish=True)["message"]  # the client said all was sent
    counts["experio.history"] = 40
    assert "integration terminee" in setup.finish_setup(557)["message"]  # a FEC: finished as before


@pytest.mark.security
def test_a_journal_of_another_dossier_is_never_given_a_bank(experio, monkeypatch):
    real = setup.odoo_call

    def odoo_call(model, method, *args, **kw):
        if model == "exp.journal" and method == "read":
            return [{"name": "BQ9", "company_id": [25, "CAFE EPSOM"], "partner_id": False}]
        return real(model, method, *args, **kw)

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    with pytest.raises(ToolError, match="n'est pas un journal du dossier `Gecafisc`"):
        setup.set_journal_bank(557, 31, 3)
    assert experio["written"] == []


@pytest.mark.functionality
def test_the_last_journals_bank_ends_the_questions(experio):
    experio["journals"].pop()
    done = setup.set_journal_bank(557, 11, 3)
    assert done["status"] == "ok" and done["dossier_id"] == 557
    assert done["message"] == "Dossier `Gecafisc` : journal `BQ1` a la banque `CREDIT IMMOBILIER ET HOTELIER (CIH)`."


@pytest.mark.functionality
def test_a_journal_with_its_tiers_keeps_it(experio, monkeypatch):
    real = setup.odoo_call

    def odoo_call(model, method, *args, **kw):
        if model == "exp.partner":
            pytest.fail("tiers looked for")
        if model == "exp.journal" and method == "read":
            return [{"name": "BQ1", "company_id": [557, "Gecafisc"], "partner_id": [8, "CIH"]}]
        return real(model, method, *args, **kw)

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    setup.set_journal_bank(557, 11, 3)
    assert experio["written"] == [{"bank_id": 3}]


@pytest.mark.security
def test_in_test_mode_the_bank_is_only_said(experio, monkeypatch):
    monkeypatch.setattr(setup, "dry_run", lambda: True)
    monkeypatch.setattr(setup, "test_mode_note", lambda: TEST_MODE)
    following = setup.set_journal_bank(557, 11, 3)
    assert experio["written"] == [] and following["message"].startswith(
        f"{TEST_MODE}Dossier `Gecafisc` : journal `BQ1`"
    )


@pytest.mark.communication
def test_experio_not_giving_the_bank_journals_is_said(experio, monkeypatch):
    monkeypatch.setattr(setup, "odoo_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(ToolError, match="`Gecafisc` : Experio n'a pas pu lire les journaux de banque"):
        setup.ask_banks(557)
    with pytest.raises(ToolError, match="`Gecafisc` : Experio a refuse la banque du journal"):
        setup.set_journal_bank(557, 11, 3)
