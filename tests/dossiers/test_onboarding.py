"""New dossiers: FEC or grand livre, the files read, and what only the client may ask for."""

import io
from datetime import datetime

import openpyxl
import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import identity
from engine.tools.dossiers.identity import ICE_WRITTEN, IF_WRITTEN, as_written, client_wrote, confirmed
from engine.tools.dossiers.setup import cell_text, entries_kind, file_text, sheet_text

HEAD = "JournalCode;JournalLib;EcritureNum;EcritureDate;CompteNum;CompteLib;EcritureLib;Debit;Credit"


@pytest.mark.functionality
def test_fec_or_grand_livre_from_the_file_alone():
    fec = HEAD + "".join(
        f"\n{j};J;{i};2025010{i % 9 + 1};{a};X;L;10;0"
        for i, (j, a) in enumerate([("AC", "6111"), ("AC", "4411"), ("BQ", "5141"), ("BQ", "4411")] * 8)
    )
    ledger = HEAD + "".join(
        f"\nAC;J;{i};20250101;{a};X;L;10;0" for i, a in enumerate(sorted(["6111", "4411", "5141", "3421"] * 8))
    )
    assert entries_kind(fec, ";") == "fec" and entries_kind(ledger, ";") == "grand livre"
    assert entries_kind(HEAD + "\nAC;J;1;20250101;6111;X;L;10;0", ";") == "fec"  # too short to tell: its headers
    assert (
        entries_kind("Code journal;Date;Compte;Libelle;Debit;Credit\nAC;01/01/2025;6111;L;10;0", ";") == "grand livre"
    )


@pytest.mark.technical
def test_text_files_in_any_usual_encoding():
    assert file_text("Débit".encode()) == "Débit" and file_text("Débit".encode("cp1252")) == "Débit"
    assert file_text(b"\xef\xbb\xbfJournalCode") == "JournalCode"


@pytest.mark.technical
def test_an_excel_file_becomes_the_text_the_import_reads():
    book = openpyxl.Workbook()
    book.active.append(["Journal", "Date", "Compte", None, "Debit", "Credit"])
    book.active.append(["ACH", datetime(2025, 1, 2), "61250000", None, 17490.0, 12.5])
    book.active.append([None, None, None, None, None, None])
    saved = io.BytesIO()
    book.save(saved)
    assert sheet_text(saved.getvalue()).splitlines() == [
        "Journal;Date;Compte;;Debit;Credit",
        "ACH;20250102;61250000;;17490;12.5",
    ]


def asked(step, message):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_results (run_id, session_id, step, status, message) values ('r', 's', %s, "
            "'question', %s)",
            (step, message),
        )


@pytest.mark.security
def test_skipping_the_ice_check_needs_a_yes_to_its_own_question_about_that_dossier(db_cabinet):
    skip = ("sans cette verification", "ALPHA")
    assert not confirmed("s", True, *skip)  # nothing confirmed before Tarik asked
    asked(
        "create_dossier",
        "Experio verifie l'ICE et l'IF aupres du registre officiel. Confirmez-vous la creation du dossier "
        "ALPHA sans cette verification ? oui / non",
    )
    assert confirmed("s", True, *skip) and not confirmed("s", False, *skip)
    assert not confirmed("s", "oui", *skip)  # the agent's reading is a boolean, never text to interpret
    assert not confirmed("s", True, "sans cette verification", "BETA")  # another dossier
    asked("assign_account", "Facture 12 : quel compte utiliser ?")
    assert not confirmed("s", True, *skip)  # this yes answers the later question, whatever its step


@pytest.mark.security
def test_a_value_counts_only_as_the_client_wrote_it():
    assert client_wrote("name", "ALPHA", "Renommez-le en Alpha.") and not client_wrote(
        "name", "ALPHA", "le code ALPHANUMERIQUE"
    )
    assert client_wrote("capital", "100000.5", "capital 100 000,50 DH") and not client_wrote(
        "capital", "100000", "capital 100 000,50 DH"
    )
    assert client_wrote("capital", "100000", "capital : 100 000 DH") and not client_wrote(
        "capital", "beaucoup", "capital 100 000"
    )
    assert client_wrote("rc", "331245", "RC 331245") and not client_wrote("rc", "33124", "RC 331245")
    assert client_wrote("ice", "1111", "Changer l'ICE du dossier a 1111") and not client_wrote(
        "ice", "1111", "ICE 001111222333444"
    )


MESSAGE = "Créez le dossier FAIR KAPPA, ICE 008123456000100 et IF n° 0712345."


@pytest.mark.functionality
def test_an_ice_or_if_sent_as_a_number_keeps_its_leading_zeros():
    assert as_written(8123456000100, ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written("8123456000100", ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written(8123456000100.0, ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written(712345, IF_WRITTEN.findall(MESSAGE)) == "0712345"


@pytest.mark.functionality
def test_a_left_out_ice_or_if_is_the_one_the_client_wrote():
    assert as_written("", ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written(None, IF_WRITTEN.findall(MESSAGE)) == "0712345"


@pytest.mark.security
def test_the_model_never_overrides_with_a_number_the_client_did_not_write():
    assert (
        as_written("001111222333444", ICE_WRITTEN.findall(MESSAGE)) == "001111222333444"
    )  # as given: create_dossier refuses it
    assert (
        as_written("", ICE_WRITTEN.findall("ICE 008123456000100 ou peut-être 008123456000999 ?")) == ""
    )  # two: no guess
    assert as_written("", ICE_WRITTEN.findall("Créez le dossier FAIR")) == ""


@pytest.mark.functionality
def test_an_if_needs_its_label_to_be_read():
    assert IF_WRITTEN.findall("IF 71234561, I.F: 0712, if n°99887766") == ["71234561", "0712", "99887766"]
    assert IF_WRITTEN.findall("tel 0612345678, ICE 008123456000100") == []
    assert IF_WRITTEN.findall("mon IF est 0712345") == ["0712345"]  # as client_wrote reads it


@pytest.mark.communication
def test_a_call_without_a_name_says_how_to_call_again():
    with pytest.raises(ToolError, match="rappelle Experio_parametrer_dossier avec dossier_id 0"):
        identity.create_dossier(client_message=MESSAGE, session_id="s")


@pytest.mark.security
def test_a_new_dossier_takes_only_the_ice_and_if_the_client_wrote(db_cabinet, monkeypatch):
    monkeypatch.setattr(identity, "dry_run", lambda: True)
    monkeypatch.setattr(identity, "lookalikes", lambda name, ice, if_number: ([], []))
    answer = identity.create_dossier(
        name="FAIR KAPPA", ice="001111222333444", if_number="0712345", client_message=MESSAGE, session_id="s"
    )
    assert answer["status"] == "ok" and answer["message"].endswith("Non repris, le client ne l'a pas ecrit : ICE.")


@pytest.mark.security
def test_an_identifier_counts_only_after_its_own_label():
    assert client_wrote("rc", "98765", "IF 12345678, RC 98765") and not client_wrote(
        "rc", "12345678", "IF 12345678, RC 98765"
    )
    assert client_wrote("if_number", "71234561", "I.F : 71 234 561") and not client_wrote(
        "if_number", "98765", "RC 98765"
    )
    assert not client_wrote("folder_type", "company", "Changez l'adresse de ma société.")
    assert client_wrote("folder_type", "company", "C'est une personne morale.")


@pytest.mark.functionality
def test_the_zeros_of_an_ice_given_earlier_come_back_on_the_yes(db_cabinet, monkeypatch):
    monkeypatch.setattr(identity, "dry_run", lambda: True)
    checked = []
    monkeypatch.setattr(identity, "lookalikes", lambda name, ice, if_number: checked.append(ice) or ([], []))
    identity.create_dossier(name="FAIR KAPPA", client_message=MESSAGE, session_id="s")  # remembered
    answer = identity.create_dossier(name="FAIR KAPPA", ice=8123456000100, client_message="oui", session_id="s")
    assert checked[-1] == "008123456000100" and "Non repris" not in answer["message"]


@pytest.mark.security
def test_the_agent_says_which_reading_of_the_clients_number_it_meant():
    assert client_wrote("capital", 1005, "capital 1,005 DH") and client_wrote("capital", 1.005, "capital 1,005 DH")
    assert not client_wrote("capital", "1,005", "capital 1,005 DH")  # still two readings: the agent must choose
    assert not client_wrote("capital", "10050", "capital 1,005 DH")


@pytest.mark.functionality
def test_a_new_dossier_is_created_from_the_main_one(db_cabinet, monkeypatch):
    """Else it takes its journals from the dossier Tarik works in, and Experio then refuses to read them."""
    monkeypatch.setattr(identity, "dry_run", lambda: False)
    monkeypatch.setattr(identity, "lookalikes", lambda name, ice, if_number: ([], []))
    monkeypatch.setattr(identity, "main_dossier_context", lambda: {"allowed_company_ids": [1]})
    monkeypatch.setattr(identity, "attach_user", lambda *a: None)
    calls = []
    monkeypatch.setattr(identity, "odoo_json", lambda *a, **k: calls.append((a, k)) or 77)
    answer = identity.create_dossier(name="FAIR KAPPA", client_message=MESSAGE, session_id="s")
    assert answer["dossier_id"] == 77 and calls[0][1] == {"context": {"allowed_company_ids": [1]}}


@pytest.mark.technical
def test_a_file_in_no_usual_encoding_is_still_read_and_an_empty_one_is_a_fec():
    assert file_text(b"D\x81bit") == "D\x81bit"  # not cp1252 either: latin-1 reads any byte
    assert cell_text(None) == "" and cell_text(17490.0) == "17490" and cell_text(12.5) == "12.5"
    assert entries_kind("", ";") == "fec"


@pytest.fixture
def creating(db_cabinet, monkeypatch):
    """Experio with ALPHA SARL (ICE 008123456000100) among its dossiers; what was created."""
    created = []
    monkeypatch.setattr(identity, "dry_run", lambda: False)
    monkeypatch.setattr(identity, "main_dossier_context", lambda: {})
    monkeypatch.setattr(identity, "attach_user", lambda user_id, dossier_id: created.append(("attach", dossier_id)))
    monkeypatch.setattr(identity, "dossiers", lambda: [{"id": 5, "name": "ALPHA SARL"}])

    def odoo_call(model, method, domain, fields):
        if method == "read":
            return [{"id": 77, "name": "FAIR KAPPA"}]
        return [{"name": "ALPHA SARL"}] if ("ice", "=", "008123456000100") in domain else []

    monkeypatch.setattr(identity, "odoo_call", odoo_call)
    monkeypatch.setattr(identity, "odoo_json", lambda model, method, values, context: created.append(values) or 78)
    return created


@pytest.mark.functionality
def test_a_dossier_this_conversation_created_is_reused_not_created_again(creating):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_results (run_id, session_id, step, status, message, dossier_id) "
            "values ('r', 's', 'create_dossier', 'ok', 'cree', 77)"
        )
    answer = identity.create_dossier(name="fair  kappa", client_message="Créez FAIR KAPPA", session_id="s")
    assert answer["reused"] is True and answer["dossier_id"] == 77 and creating == []
    other = identity.create_dossier(name="DELTA", client_message="Créez DELTA", session_id="s")
    assert other["dossier_id"] == 78 and creating[0]["name"] == "DELTA"  # another name: a new dossier


def sent_fec(session="s"):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data) "
            "values (%s, 'FEC ANFA RETAIL.xlsx', 'application/vnd.ms-excel', 'x')",
            (session,),
        )


@pytest.mark.functionality
def test_a_name_that_is_only_a_files_name_reuses_the_dossier_the_conversation_created(creating):
    """The client said « charge ce FEC »: « ANFA RETAIL » is the file's, not a dossier to create."""
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_results (run_id, session_id, step, status, message, dossier_id) "
            "values ('r', 's', 'create_dossier', 'ok', 'cree', 77)"
        )
    sent_fec()
    answer = identity.create_dossier(name="ANFA RETAIL", client_message="Merci de charger ce FEC", session_id="s")
    assert answer["reused"] is True and answer["dossier_id"] == 77 and creating == []


@pytest.mark.functionality
def test_a_name_that_is_only_a_files_name_asks_for_the_dossiers_name_when_none_was_created(creating):
    sent_fec()
    answer = identity.create_dossier(name="ANFA RETAIL", client_message="Merci de charger ce FEC", session_id="s")
    assert answer["status"] == "question" and "nom d'un fichier" in answer["message"] and creating == []


@pytest.mark.functionality
def test_a_name_the_client_wrote_is_created_even_when_their_file_carries_it(creating):
    sent_fec()
    answer = identity.create_dossier(
        name="ANFA RETAIL", client_message="Créez le dossier ANFA RETAIL, voici son FEC", session_id="s"
    )
    assert answer["dossier_id"] == 78 and creating[0]["name"] == "ANFA RETAIL"


@pytest.mark.security
def test_no_dossier_is_created_with_another_dossiers_ice(creating):
    answer = identity.create_dossier(
        name="BETA", ice="008123456000100", client_message="Créez BETA, ICE 008123456000100", session_id="s"
    )
    assert answer["status"] == "stop" and "(`ALPHA SARL`)" in answer["message"] and creating == []


@pytest.mark.security
def test_a_very_similar_name_is_created_only_on_the_clients_yes(creating):
    asked_first = identity.create_dossier(name="ALPHA SARLL", client_message="Créez ALPHA SARLL", session_id="s")
    assert asked_first["status"] == "question" and "(`ALPHA SARL`)" in asked_first["message"] and creating == []
    asked("create_dossier", asked_first["message"])
    done = identity.create_dossier(name="ALPHA SARLL", client_confirmed=True, client_message="oui", session_id="s")
    assert done["dossier_id"] == 78 and creating == [
        {"name": "ALPHA SARLL", "folder_type": "company", "enabled": True},
        ("attach", 78),
    ]


@pytest.mark.security
def test_a_new_dossier_skips_the_registry_check_only_on_the_clients_yes(creating):
    message = "Créez GAMMA, ICE 008123456000999, sans verification"
    first = identity.create_dossier(
        name="GAMMA", ice="008123456000999", skip_ice_if_check=True, client_message=message, session_id="s"
    )
    assert first["status"] == "question" and "creation du dossier `GAMMA` sans cette verification" in first["message"]
    asked("create_dossier", first["message"])
    done = identity.create_dossier(
        name="GAMMA",
        ice="008123456000999",
        skip_ice_if_check=True,
        client_confirmed=True,
        client_message="oui",
        session_id="s",
    )
    assert creating[0]["ignore_ice_if_verification"] is True and creating[0]["ice"] == "008123456000999"
    assert done["message"].endswith("ICE et IF non verifies (verification ignoree, a la demande du client).")


@pytest.mark.security
def test_an_if_the_client_did_not_write_is_left_out_of_a_new_dossier(creating):
    answer = identity.create_dossier(
        name="DELTA", if_number="99887766", client_message="Créez DELTA, IF 0712345", session_id="s"
    )
    assert "vat" not in creating[0] and answer["message"].endswith("Non repris, le client ne l'a pas ecrit : IF.")


@pytest.mark.functionality
def test_an_unknown_dossier_type_is_refused(creating):
    with pytest.raises(ToolError, match="Type de dossier inconnu"):
        identity.create_dossier(name="DELTA", folder_type="sarl", client_message="Créez DELTA", session_id="s")
    assert creating == []


@pytest.mark.communication
def test_experio_refusing_a_new_dossier_gives_its_reason(creating, monkeypatch):
    monkeypatch.setattr(identity, "odoo_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ICE invalide")))
    with pytest.raises(ToolError, match="Experio refuse de creer le dossier `DELTA` : ICE invalide"):
        identity.create_dossier(name="DELTA", client_message="Créez DELTA", session_id="s")
