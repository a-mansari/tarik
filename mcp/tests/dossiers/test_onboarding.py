"""New dossiers: FEC or grand livre, the files read, and what only the client may ask for."""

import io
from datetime import datetime

import openpyxl

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.dossiers import onboarding
from tarik.dossiers.onboarding import ICE_WRITTEN, IF_WRITTEN, as_written, copy_asked, entries_kind, file_text, sheet_text, skip_agreed

HEAD = "JournalCode;JournalLib;EcritureNum;EcritureDate;CompteNum;CompteLib;EcritureLib;Debit;Credit"


def test_fec_or_grand_livre_from_the_file_alone():
    fec = HEAD + "".join(f"\n{j};J;{i};2025010{i % 9 + 1};{a};X;L;10;0" for i, (j, a) in enumerate(
        [("AC", "6111"), ("AC", "4411"), ("BQ", "5141"), ("BQ", "4411")] * 8))
    ledger = HEAD + "".join(f"\nAC;J;{i};20250101;{a};X;L;10;0" for i, a in enumerate(sorted(["6111", "4411", "5141", "3421"] * 8)))
    assert entries_kind(fec, ";") == "fec" and entries_kind(ledger, ";") == "grand livre"
    assert entries_kind(HEAD + "\nAC;J;1;20250101;6111;X;L;10;0", ";") == "fec"  # too short to tell: its headers
    assert entries_kind("Code journal;Date;Compte;Libelle;Debit;Credit\nAC;01/01/2025;6111;L;10;0", ";") == "grand livre"


def test_text_files_in_any_usual_encoding():
    assert file_text("Débit".encode("utf-8")) == "Débit" and file_text("Débit".encode("cp1252")) == "Débit"
    assert file_text(b"\xef\xbb\xbfJournalCode") == "JournalCode"


def test_an_excel_file_becomes_the_text_the_import_reads():
    book = openpyxl.Workbook()
    book.active.append(["Journal", "Date", "Compte", None, "Debit", "Credit"])
    book.active.append(["ACH", datetime(2025, 1, 2), "61250000", None, 17490.0, 12.5])
    book.active.append([None, None, None, None, None, None])
    saved = io.BytesIO()
    book.save(saved)
    assert sheet_text(saved.getvalue()).splitlines() == ["Journal;Date;Compte;;Debit;Credit", "ACH;20250102;61250000;;17490;12.5"]


def test_skipping_the_ice_check_only_when_the_client_asks():
    assert skip_agreed("Créez-le sans vérification") and skip_agreed("Ignorez la vérification de l'ICE") and skip_agreed("pas besoin de vérifier")
    assert not skip_agreed("Les numéros sont corrects, ils viennent de nous être attribués") and not skip_agreed("Je confirme, créez-le")
    assert not skip_agreed("") and not skip_agreed("sans modèle")


def test_copying_a_configuration_only_when_the_client_asks():
    assert copy_asked(["Ouvrez VEGA, avec la même configuration que le dossier LYJA."], "LYJA (Test)")
    assert copy_asked(["bonjour", "copiez le paramétrage de Alpha Négoce"], "ALPHA NEGOCE SARL")
    assert not copy_asked(["Ouvrez-nous un dossier VEGA SARL, ICE 1111", "oui"], "LYJA (Test)")
    assert not copy_asked(["comme d'habitude"], "LYJA (Test)") and not copy_asked(["le dossier LYJA"], "LYJA (Test)")


MESSAGE = "Créez le dossier FAIR KAPPA, ICE 008123456000100 et IF n° 0712345."


def test_an_ice_or_if_sent_as_a_number_keeps_its_leading_zeros():
    assert as_written(8123456000100, ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written("8123456000100", ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written(8123456000100.0, ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written(712345, IF_WRITTEN.findall(MESSAGE)) == "0712345"


def test_a_left_out_ice_or_if_is_the_one_the_client_wrote():
    assert as_written("", ICE_WRITTEN.findall(MESSAGE)) == "008123456000100"
    assert as_written(None, IF_WRITTEN.findall(MESSAGE)) == "0712345"


def test_the_model_never_overrides_with_a_number_the_client_did_not_write():
    assert as_written("001111222333444", ICE_WRITTEN.findall(MESSAGE)) == "001111222333444"  # kept, Experio checks it
    assert as_written("", ICE_WRITTEN.findall("ICE 008123456000100 ou peut-être 008123456000999 ?")) == ""  # two: no guess
    assert as_written("", ICE_WRITTEN.findall("Créez le dossier FAIR")) == ""


def test_an_if_needs_its_label_to_be_read():
    assert IF_WRITTEN.findall("IF 71234561, I.F: 0712, if n°99887766") == ["71234561", "0712", "99887766"]
    assert IF_WRITTEN.findall("tel 0612345678, ICE 008123456000100") == []


def test_a_call_without_a_name_says_how_to_call_again(monkeypatch):
    monkeypatch.setattr(onboarding, "remember_client_message", lambda *args: None)
    with pytest.raises(ToolError, match="rappelle parametrer_dossier avec dossier_id 0"):
        onboarding.create_dossier(client_message=MESSAGE, session_id="s")
