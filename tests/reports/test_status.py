"""The dossier status report as a tool: who may ask for it, what it refuses before reading Experio, and the files it
leaves in the conversation's outbox."""

import io

import pytest
from docx import Document as WordDocument

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.reports import status

FOUND = {
    "name": "ALPHA",
    "exercice": {"name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31"},
    "progress": (80, 8, 10, 4, 5),
    "states": {("purchase", "post"): 6, ("purchase", "draft"): 2, ("sale", "post"): 3},
    "payments": {("purchase", "paid"): 6, ("sale", "unpaid"): 2, ("sale", "paid"): 1},
    "statements": {"exp.bank.statement": ({"post": 1}, {"post": 4}), "exp.monetary.statement": ({}, {})},
    "suspense": 1,
    "year": 2026,
    "declarations": [("TVA", {7: [{"period_id": [1, "T1"], "state": "done"}]})],
}


@pytest.fixture
def experio(monkeypatch):
    """Experio holds dossier 7 (FOUND); who is asking is ``allowed``; ``reads`` counts the dossiers read."""
    reads, allowed = [], {"status": "routine", "message": "ok"}
    monkeypatch.setattr(status, "check_sender", lambda session_id, dossier_id: allowed)
    monkeypatch.setattr(status, "status_of", lambda dossier_id, year=0: reads.append((dossier_id, year)) or dict(FOUND))
    return reads, allowed


def outbox(session):
    with cabinet_db() as db:
        return db.execute(
            "select file_name, data from tarik_outbox where session_id = %s order by id", (session,)
        ).fetchall()


@pytest.mark.functionality
def test_the_report_is_kept_in_the_conversation_in_every_format_asked(db_cabinet, experio):
    text = status.make_report("s1", "statut", 7, ["docx", "xlsx", "pdf"], 2026, "")
    kept = outbox("s1")
    assert [name for name, _ in kept] == [
        "etat-du-dossier-alpha.docx",
        "etat-du-dossier-alpha.xlsx",
        "etat-du-dossier-alpha.pdf",
    ]
    assert text.startswith("Fichiers prêts : etat-du-dossier-alpha.docx") and experio[0] == [(7, 2026)]
    word = WordDocument(io.BytesIO(bytes(kept[0][1])))
    assert word.core_properties.title == "État du dossier ALPHA" and "Points d'attention" in [
        p.text for p in word.paragraphs
    ]
    assert any(cell.text == "80 %" for table in word.tables for row in table.rows for cell in row.cells)
    assert outbox("s2") == []


@pytest.mark.security
def test_a_sender_who_may_not_have_the_dossier_gets_no_report_and_nothing_is_read(db_cabinet, experio):
    reads, allowed = experio
    allowed.update(
        status="stop", message="Le dossier `ALPHA` n'est pas attribué à `Marie` dans Experio : je n'y fais rien."
    )
    with pytest.raises(ToolError, match="n'est pas attribué"):
        status.make_report("s1", "statut", 7, ["docx"])
    assert reads == [] and outbox("s1") == []


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("kind", "formats", "message"),
    [("bilan", ["docx"], "pas « bilan »"), ("statut", ["rtf"], "pas « rtf »"), ("statut", [], "parmi")],
)
def test_a_wrong_kind_or_format_is_refused_before_experio_is_read(db_cabinet, experio, kind, formats, message):
    with pytest.raises(ToolError, match=message):
        status.make_report("s1", kind, 7, formats)
    assert experio[0] == [] and outbox("s1") == []


@pytest.mark.communication
def test_the_report_says_where_its_figures_come_from():
    from datetime import date

    doc = status.status_document({**FOUND, "id": 7}, date(2026, 11, 9))
    assert "Chiffres lus dans Experio le 09/11/2026" in doc.blocks[-1]["text"]
    assert (
        "2 factures d'achat en brouillon, à vérifier." in next(b for b in doc.blocks if b["type"] == "bullets")["items"]
    )


@pytest.mark.security
def test_a_full_outbox_a_wrong_year_or_no_conversation_cost_no_experio_read(db_cabinet, experio):
    reads, _ = experio
    for n in range(5):
        status.make_report("s1", "statut", 7, ["docx", "pdf", "md"], 0, f"r{n}")  # 15 files wait
    assert len(reads) == 5
    for session, year, message in [
        ("s1", 0, "Trop de fichiers"),
        ("s2", 1500, "entre 1990 et 2100"),
        ("", 0, "conversation"),
    ]:
        with pytest.raises(ToolError, match=message):
            status.make_report(session, "statut", 7, ["docx"], year)
    assert len(reads) == 5  # none of the three read Experio
