"""Zip files clients send (tarik.core.archives): each replaced by the files in it Tarik takes, nothing in it trusted:
a path cut to its name, sizes read never believed, a zip in a zip opened once, what is left out said."""

import io
import zipfile

import pytest

from tarik.core import archives
from tarik.core.config import FILE_TYPES


def zipped(*entries, flags=0):
    """A zip of ``entries`` [(name, bytes)]; ``flags`` set on each in its headers (0x1: marked encrypted, as a
    password-protected archive is: zipfile itself never writes one)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    data = bytearray(buffer.getvalue())
    for signature, at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # each local header, each central one
        start = data.find(signature)
        while start != -1:
            data[start + at] |= flags
            start = data.find(signature, start + 4)
    return bytes(data)


def names(kept):
    return [name for name, _, _ in kept]


@pytest.mark.functionality
def test_a_zip_becomes_the_files_in_it_tarik_takes():
    fec = b"JournalCode;EcritureDate;CompteNum;Debit\nAC;20260101;6111;100\n"
    data = zipped(("factures/f1.pdf", b"%PDF-1"), ("scan.png", b"png"), ("fec.csv", fec), ("virus.exe", b"MZ"))
    kept, skipped = archives.unpacked(
        [("lot.zip", "application/zip", data), ("seul.pdf", "application/pdf", b"%")], FILE_TYPES
    )
    assert names(kept) == ["lot.zip › f1.pdf", "lot.zip › scan.png", "lot.zip › fec.csv", "seul.pdf"]
    assert [mime for _, mime, _ in kept[:2]] == ["application/pdf", "image/png"] and kept[0][2] == b"%PDF-1"
    assert skipped == ["lot.zip › virus.exe : type non pris en charge"]


@pytest.mark.security
def test_nothing_in_a_zip_reaches_outside_its_name():
    data = zipped(
        ("../../etc/passwd.pdf", b"x"),
        ("C:\\\\Windows\\\\boot.pdf", b"y"),
        ("__MACOSX/._f1.pdf", b"z"),
        (".DS_Store", b"w"),
        ("dossier/", b""),
    )
    kept, skipped = archives.unpacked([("a.zip", "application/zip", data)], FILE_TYPES)
    assert names(kept) == ["a.zip › passwd.pdf", "a.zip › boot.pdf"] and skipped == []


@pytest.mark.security
def test_a_zip_bomb_stops_at_the_limit(monkeypatch):
    monkeypatch.setattr(archives, "MAX_FILE", 1000)
    data = zipped(("enorme.pdf", b"0" * 50_000), ("petit.pdf", b"ok"))  # 50 kB of zeros: a few hundred bytes zipped
    kept, skipped = archives.unpacked([("b.zip", "application/zip", data)], FILE_TYPES)
    assert names(kept) == ["b.zip › petit.pdf"] and len(skipped) == 1
    assert skipped[0].startswith("b.zip › enorme.pdf : plus de") and "au plus par fichier" in skipped[0]


@pytest.mark.security
def test_an_archive_gives_at_most_so_many_files_and_bytes(monkeypatch):
    monkeypatch.setattr(archives, "MAX_FILES", 3)
    data = zipped(*[(f"f{i}.pdf", b"x") for i in range(5)])
    kept, skipped = archives.unpacked([("c.zip", "application/zip", data)], FILE_TYPES)
    assert len(kept) == 3 and skipped == [
        "c.zip : plus de 3 fichiers, les suivants ignorés (envoyez-les dans une autre archive)"
    ]
    monkeypatch.setattr(archives, "MAX_FILES", 100)
    monkeypatch.setattr(archives, "MAX_TOTAL", 5)
    kept, skipped = archives.unpacked(
        [("d.zip", "application/zip", zipped(("a.pdf", b"123"), ("b.pdf", b"456")))], FILE_TYPES
    )
    assert names(kept) == ["d.zip › a.pdf"] and len(skipped) == 1  # the archive's total, not the file's own size
    assert skipped[0].startswith("d.zip › b.pdf : l'archive dépasse") and "autre archive" in skipped[0]


@pytest.mark.security
def test_a_zip_in_a_zip_is_opened_once_never_deeper():
    deepest = zipped(("trop.pdf", b"x"))
    inner = zipped(("f2.pdf", b"2"), ("encore.zip", deepest))
    kept, skipped = archives.unpacked(
        [("e.zip", "application/zip", zipped(("f1.pdf", b"1"), ("sous.zip", inner)))], FILE_TYPES
    )
    assert names(kept) == ["e.zip › f1.pdf", "e.zip › sous.zip › f2.pdf"]
    assert skipped == [
        "e.zip › sous.zip › encore.zip : archive dans une archive dans une archive (une seule archive dans "
        "l'archive au plus)"
    ]


@pytest.mark.security
def test_a_protected_or_broken_archive_is_said_never_guessed():
    kept, skipped = archives.unpacked(
        [
            ("p.zip", "application/zip", zipped(("f.pdf", b"x"), flags=0x1)),
            ("cassé.zip", "application/zip", b"PK\x03\x04 pas une archive"),
        ],
        FILE_TYPES,
    )
    assert kept == [] and skipped == ["p.zip › f.pdf : protégé par un mot de passe", "cassé.zip : archive illisible"]


@pytest.mark.functionality
def test_a_text_file_in_a_zip_is_kept_only_when_it_holds_entries():
    fec = "JournalCode\tJournalLib\tEcritureNum\tEcritureDate\tCompteNum\n" + "AC\tAchats\t1\t20260101\t6111\n"
    ledger = "date;compte;libelle;debit;credit\n01/01/2026;6111;achat;100;0\n"
    data = zipped(("fec.txt", fec.encode()), ("gl.csv", ledger.encode("latin-1")), ("notes.txt", b"bonjour"))
    kept, skipped = archives.unpacked([("lot.zip", "application/zip", data)], FILE_TYPES)
    assert names(kept) == ["lot.zip › fec.txt", "lot.zip › gl.csv"]
    assert skipped == ["lot.zip › notes.txt : texte sans écritures comptables (ni FEC ni grand livre)"]


@pytest.mark.functionality
def test_a_text_file_sent_alone_is_kept_only_when_it_holds_entries():
    fec = b"JournalCode|JournalLib|EcritureNum|CompteNum\nAC|Achats|1|6111\n"
    files = [
        ("FEC 2025.txt", "text/plain", fec),
        ("lisez-moi.txt", "text/plain", b"Merci de traiter."),
        ("f.pdf", "", b"%"),
    ]
    kept, skipped = archives.unpacked(files, FILE_TYPES)
    assert names(kept) == ["FEC 2025.txt", "f.pdf"]
    assert skipped == ["lisez-moi.txt : texte sans écritures comptables (ni FEC ni grand livre)"]


@pytest.mark.functionality
def test_an_empty_archive_is_said_never_silently_lost():
    kept, skipped = archives.unpacked(
        [
            ("vide.zip", "application/zip", zipped()),
            ("mac.zip", "application/zip", zipped((".DS_Store", b"w"), ("__MACOSX/._f.pdf", b"z"))),
            ("lot.zip", "application/zip", zipped(("f.pdf", b"%"), ("dedans.zip", zipped()))),
        ],
        FILE_TYPES,
    )
    assert names(kept) == ["lot.zip › f.pdf"]
    assert skipped == ["vide.zip : archive vide", "mac.zip : archive vide", "lot.zip › dedans.zip : archive vide"]


@pytest.mark.functionality
def test_a_zip_sent_to_the_agent_is_saved_as_its_files_once(db_cabinet):
    import base64

    from tarik.invoices.pipeline import save_files

    data = base64.b64encode(zipped(("f1.pdf", b"1"), ("f2.pdf", b"2"))).decode()
    files = [{"id": "zip-1", "file_name": "lot.zip", "mime_type": "application/zip", "data": data}]
    first = save_files(session_id="s-zip", files=files)
    assert [f["file_name"] for f in first["files"]] == ["lot.zip › f1.pdf", "lot.zip › f2.pdf"]
    assert save_files(session_id="s-zip", files=files)["files"] == []  # the sequence run again: saved once
