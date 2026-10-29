"""A dossier set up from separate files: what each file is, the bank a client names, the bank of each bank
journal asked and set, and an onboarding that waits for the files until the client says all were sent."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
from engine.tools.dossiers import banks, setup
from engine.tools.dossiers.setup import bank_named, file_kind

BANKS = [  # as Experio lists them (res.bank name and code)
    {"id": 3, "name": "CREDIT IMMOBILIER ET HOTELIER (CIH)", "code": "cih"},
    {"id": 4, "name": "ATTIJARIWAFA BANK", "code": "attijari"},
    {"id": 5, "name": "BANQUE CENTRALE POPULAIRE (BP)", "code": "bp"},
    {"id": 6, "name": "SAHAM BANK", "code": "sgeneral"},
    {"id": 7, "name": "BANK OF AFRICA (BMCE)", "code": "bmce"},
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
    assert bank_named("Chaabi", BANKS)["id"] == 5 and bank_named("AWB", BANKS)["id"] == 4
    assert bank_named("la banque", BANKS) is None and bank_named("BQ1", BANKS) is None
    two_credits = [{"id": 8, "name": "CREDIT AGRICOLE DU MAROC"}, {"id": 9, "name": "CREDIT DU MAROC"}]
    assert bank_named("BQ CREDIT", [*two_credits, {"id": 10, "name": "CREDIT IMMOBILIER"}]) is None  # a tie: asked
    assert bank_named("CAM", two_credits)["id"] == 8 and bank_named("CDM", two_credits)["id"] == 9
    assert bank_named("relevé Crédit du Maroc", two_credits)["id"] == 9  # its whole name written: no tie


@pytest.mark.functionality
def test_the_bank_a_rib_says_by_its_first_three_digits():
    assert banks.bank_of_rib("007780000937200000020230", BANKS)["id"] == 4
    assert banks.bank_of_rib("RIB : 007 780 00 00082000020894 21", BANKS)["id"] == 4  # grouped, in a sentence
    assert banks.bank_of_rib("011 810 0000012345678901 23", BANKS)["id"] == 7
    assert banks.bank_of_rib("145 450 2111122223333444 55", BANKS)["id"] == 5  # a regional Banque Populaire
    assert banks.bank_of_rib("00778000339100000283323", BANKS) is None  # 23 digits: no RIB
    assert banks.bank_of_rib("999780000937200000020230", BANKS) is None  # a code not listed: the name decides
    assert banks.bank_of_rib("51410000", BANKS) is None and banks.bank_of_rib(None, BANKS) is None
    assert banks.bank_of_rib("MA12 007 780 0000082000020894 21", BANKS)["id"] == 4  # an IBAN: 12 is no bank code
    assert banks.bank_of_rib("MA64007780000937200000020230", BANKS)["id"] == 4  # an IBAN without spaces


@pytest.mark.functionality
def test_a_rib_of_a_bank_not_listed_leaves_the_name_to_decide():
    assert banks.journal_bank({"name": "BQ BMCE", "rib": "999780000937200000020230"}, BANKS) == (BANKS[4], "son nom")
    assert banks.journal_bank({"name": "BQ BMCE", "rib": False}, BANKS) == (BANKS[4], "son nom")  # as Odoo gives none
    assert banks.journal_bank({"name": "BQ 230 780 0000012345678901 23", "rib": False}, BANKS) == (BANKS[0], "son RIB")
    assert banks.journal_bank({"name": "BQ1", "rib": False}, BANKS) == (None, "")


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
    monkeypatch.setattr(banks, "odoo_call", odoo_call)
    monkeypatch.setattr(banks, "dry_run", lambda: setup.dry_run())  # as the test sets it, now or later
    return state


@pytest.mark.communication
def test_each_bank_journal_is_asked_its_bank_in_turn(experio):
    asked = setup.ask_banks(557)
    assert asked["status"] == "question" and asked["journal_id"] == 11 and asked["choices"][3].startswith("CREDIT")
    assert "à quelle banque est le journal `BQ1`" in asked["message"]
    assert "Il reste ensuite 1 autre journal à renseigner." in asked["message"]
    following = setup.set_journal_bank(557, 11, 3)
    assert experio["written"] == [{"bank_id": 3, "partner_id": 99}]  # the bank, and the tiers named after it
    assert following["status"] == "question" and following["journal_id"] == 12  # the next one, in the same reply
    assert following["message"].startswith("Dossier `Gecafisc` : journal `BQ1` a la banque `CREDIT")
    experio["journals"].clear()
    assert setup.ask_banks(557)["routine"]


@pytest.mark.communication
@pytest.mark.parametrize("name", ["Banque ATTIJARIWAFA BANK", "BQ AWB", "Attijari"])
def test_a_bank_the_journals_name_says_is_set_and_said_not_asked(experio, name):
    experio["journals"][0]["name"] = name
    result = setup.ask_banks(557)
    assert experio["written"] == [{"bank_id": 4, "partner_id": 99}]
    assert "a la banque `ATTIJARIWAFA BANK` (d'après son nom" in result["message"]
    assert result["status"] == "question" and result["journal_id"] == 12  # BQ2 names none: asked


@pytest.mark.communication
def test_a_bank_the_journals_rib_says_is_set_even_over_its_name(experio):
    experio["journals"][0].update(name="Banque CIH", rib="007 780 00 00082000020894 21")
    result = setup.ask_banks(557)
    assert experio["written"] == [{"bank_id": 4, "partner_id": 99}]  # Attijariwafa's RIB, whatever the name says
    assert "a la banque `ATTIJARIWAFA BANK` (d'après son RIB" in result["message"]


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
    monkeypatch.setattr(banks, "odoo_call", odoo_call)
    monkeypatch.setattr(banks, "dry_run", lambda: setup.dry_run())  # as the test sets it, now or later
    waiting = setup.finish_setup(557)
    assert "Il manque : journaux, fournisseurs, clients." in waiting["message"]
    assert "l'intégration serait terminée" in setup.finish_setup(557, finish=True)["message"]  # all sent
    counts["exp.journal"] = 9
    finished = setup.finish_setup(557)["message"]  # its accounts and journals (a model's copy)
    assert "l'intégration serait terminée" in finished
    assert finished.endswith("\n\nVous pouvez encore envoyer vos fichiers fournisseurs et clients.")
    assert "Vous pouvez" not in setup.finish_setup(557, finish=True)["message"]  # the client said all was sent
    counts["exp.journal"] = 0
    counts["experio.history"] = 40
    assert "l'intégration serait terminée" in setup.finish_setup(557)["message"]  # a FEC: finished as before


@pytest.mark.security
def test_a_journal_of_another_dossier_is_never_given_a_bank(experio, monkeypatch):
    real = setup.odoo_call

    def odoo_call(model, method, *args, **kw):
        if model == "exp.journal" and method == "read":
            return [{"name": "BQ9", "company_id": [25, "CAFE EPSOM"], "partner_id": False}]
        return real(model, method, *args, **kw)

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    monkeypatch.setattr(banks, "odoo_call", odoo_call)
    monkeypatch.setattr(banks, "dry_run", lambda: setup.dry_run())  # as the test sets it, now or later
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
    monkeypatch.setattr(banks, "odoo_call", odoo_call)
    monkeypatch.setattr(banks, "dry_run", lambda: setup.dry_run())  # as the test sets it, now or later
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
    with pytest.raises(ToolError, match="`Gecafisc` : Experio a refusé la banque du journal"):
        setup.set_journal_bank(557, 11, 3)
