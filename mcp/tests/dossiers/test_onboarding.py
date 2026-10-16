"""New dossiers: FEC or grand livre, the files read, and what only the client may ask for."""

import io
from datetime import datetime

import openpyxl

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.dossiers import identity
from tarik.core.cabinets import cabinet_db
from tarik.dossiers.identity import ICE_WRITTEN, IF_WRITTEN, as_written, client_wrote, confirmed
from tarik.dossiers.setup import entries_kind, file_text, sheet_text

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
    assert file_text("Débit".encode("utf-8")) == "Débit" and file_text("Débit".encode("cp1252")) == "Débit"
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
    assert not confirmed("s", "Créez-le sans vérification", *skip)  # asking is not confirming: Tarik asks first
    asked(
        "create_dossier",
        "Experio verifie l'ICE et l'IF aupres du registre officiel. Confirmez-vous la creation du dossier "
        "ALPHA sans cette verification ? oui / non",
    )
    assert confirmed("s", "Oui, je confirme", *skip) and not confirmed("s", "non", *skip)
    assert not confirmed(
        "s", "Oui, créez aussi BETA sans vérification", "sans cette verification", "BETA"
    )  # another dossier
    asked("assign_account", "Facture 12 : quel compte utiliser ?")
    assert not confirmed("s", "oui", *skip)  # this yes answers the later question, whatever its step


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
