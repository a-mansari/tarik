"""The documents Tarik makes: what a document may hold, each format read back, what a client's or a model's text can
never become (a formula, a LaTeX command, markup), and the tool that keeps the files in the conversation's outbox."""

import io
import re

import openpyxl
import pypdfium2
import pytest
from docx import Document as WordDocument

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.reports import document, files, formats

BLOCKS = [
    {"type": "heading", "level": 1, "text": "Synthèse"},
    {"type": "paragraph", "text": "Total : 1 234,56 MAD."},
    {"type": "note", "text": "Analyse automatique, à valider."},
    {"type": "bullets", "items": ["un", "deux"]},
    {"type": "numbered", "items": ["a", "b"]},
    {
        "type": "table",
        "caption": "Ventes [T1]: mois",
        "headers": ["Mois", "Montant", "Code"],
        "rows": [["Janvier", "1 234,56", "007"], ["=1+1", "-12.5", "@SUM(A1)"], ["Févr|ier", 3, None]],
    },
]


def made(blocks=BLOCKS, title="Rapport « test »", subtitle="Dossier ALPHA"):
    return document.parse(title, blocks, subtitle)


@pytest.mark.functionality
def test_a_document_holds_clean_text_and_numbers_and_says_which_block_is_wrong():
    doc = made(
        [{"type": "paragraph", "text": " a\x00b\r\nc "}, {"type": "table", "headers": ["n"], "rows": [[1.5], [None]]}]
    )
    assert doc.blocks[0]["text"] == "ab\nc" and doc.blocks[1]["rows"] == [["1.5"], [""]]
    bad = [
        ([{"type": "image"}], "Bloc 1"),
        ([{"type": "paragraph", "text": "x" * 4001}], "Bloc 1 : 4000"),
        ([{"type": "heading", "level": 4, "text": "x"}], "niveau"),
        ([{"type": "table", "headers": ["a", "b"], "rows": [["1", "2"], ["3"]]}], "Bloc 1, ligne 2 : 2 cellules"),
        ([{"type": "table", "headers": [], "rows": []}], "en-têtes"),
        ([{"type": "table", "headers": ["a"], "rows": [[True]]}], "texte ou un nombre"),
        ([{"type": "paragraph", "text": "x"}] * 301, "300"),
        ("pas une liste", "liste"),
    ]
    for blocks, message in bad:
        with pytest.raises(ToolError, match=message):
            document.parse("T", blocks)
    with pytest.raises(ToolError, match="titre"):
        document.parse("  ", [])


@pytest.mark.functionality
def test_markdown_word_and_pdf_read_back_what_was_written():
    doc = made()
    md = formats.render_md(doc)[0].decode()
    assert md.startswith("# Rapport « test »") and "| Févr\\|ier | 3 |  |" in md and "> Analyse automatique" in md
    word = WordDocument(io.BytesIO(formats.render_docx(doc)[0]))
    assert word.core_properties.title == "Rapport « test »" and word.core_properties.author == "Tarik"
    texts = [p.text for p in word.paragraphs]
    assert "Synthèse" in texts and "1. a" in texts and "Analyse automatique, à valider." in texts
    table = word.tables[0]
    assert [c.text for c in table.rows[0].cells] == ["Mois", "Montant", "Code"] and table.rows[0].cells[0].paragraphs[
        0
    ].runs[0].bold
    assert table.rows[1].cells[1].text == "1 234,56" and table.rows[2].cells[0].text == "=1+1"
    pdf, notes = formats.render_pdf(doc)
    page = pypdfium2.PdfDocument(pdf)[0].get_textpage().get_text_range()
    assert "Rapport « test »" in page and "Janvier" in page and "Page 1" in page and notes == []


@pytest.mark.security
def test_in_excel_a_formula_is_text_numbers_are_numbers_and_codes_keep_their_zeros():
    book = openpyxl.load_workbook(io.BytesIO(formats.render_xlsx(made())[0]))
    assert book.sheetnames == ["Rapport", "Ventes T1 mois"]  # nothing Excel forbids in a sheet's name
    sheet = book["Ventes T1 mois"]
    assert sheet["B2"].value == 1234.56 and sheet["B3"].value == -12.5 and sheet["B4"].value == 3
    assert sheet["C2"].value == "007" and sheet["A3"].value == "=1+1" and sheet["A3"].data_type == "s"
    assert sheet["C3"].value == "@SUM(A1)" and sheet["C3"].data_type == "s" and sheet["A1"].font.bold
    main = [cell.value for cell in book["Rapport"]["A"] if cell.value]
    assert main[0] == "Rapport « test »" and "• un" in main and "2. b" in main
    assert any("onglet « Ventes T1 mois »" in text for text in main)  # where the table went


@pytest.mark.security
def test_in_latex_no_text_can_become_a_command():
    attack = r"\input{/etc/passwd} \write18{rm} $x$ & # _ % ^ ~ {y}"
    doc = made(
        [{"type": "paragraph", "text": attack}, {"type": "table", "headers": [r"\h"], "rows": [[r"\input{z}"]]}],
        r"\title{x}",
    )
    source = formats.render_tex(doc)[0].decode()
    body = source.split(r"\begin{document}")[1]
    for command in (r"\input{", r"\write", r"\title{x}"):
        assert command not in body, command
    assert (
        r"\textbackslash{}input\{/etc/passwd\}" in body
        and r"\$x\$ \& \# \_ \% \textasciicircum{} \textasciitilde{} \{y\}" in body
    )
    assert r"\textbackslash{}h" in body
    assert source.count(r"\begin{document}") == 1 and source.count(r"\end{document}") == 1


@pytest.mark.functionality
def test_a_pdf_says_what_its_font_could_not_hold():
    pdf, notes = formats.render_pdf(made([{"type": "paragraph", "text": "Client عربى"}]))
    assert len(notes) == 1 and "4 caractère" in notes[0]
    assert "Client ????" in pypdfium2.PdfDocument(pdf)[0].get_textpage().get_text_range()


@pytest.mark.functionality
def test_a_large_table_stays_within_limits_and_is_written_in_every_format():
    rows = [[f"Ligne {n}", n * 1.5, "x"] for n in range(2000)]
    doc = made([{"type": "table", "headers": ["a", "b", "c"], "rows": rows}])
    for name, render in formats.RENDERERS.items():
        data, _ = render(doc)
        assert 0 < len(data) < formats.MAX_BYTES, name
    with pytest.raises(ToolError, match="2000"):
        made([{"type": "table", "headers": ["a"], "rows": [["x"]] * 2001}])


def outbox(session):
    with cabinet_db() as db:
        return db.execute(
            "select file_name, mime_type, length(data) from tarik_outbox where session_id = %s order by id", (session,)
        ).fetchall()


@pytest.mark.functionality
def test_the_tool_keeps_each_file_in_the_conversations_outbox_and_says_so(db_cabinet):
    text = files.make_document(
        "s1", "Rapport d'été 2026", BLOCKS, ["DOCX", " xlsx", "pdf", "docx"], "Dossier ALPHA", ""
    )
    kept = outbox("s1")
    assert [name for name, _, _ in kept] == [
        "rapport-d-ete-2026.docx",
        "rapport-d-ete-2026.xlsx",
        "rapport-d-ete-2026.pdf",
    ]
    assert (
        kept[0][1].endswith("wordprocessingml.document")
        and kept[2][1] == "application/pdf"
        and all(size > 0 for *_, size in kept)
    )
    assert text.startswith("Fichiers prêts : rapport-d-ete-2026.docx (") and "joints à ma réponse" in text
    assert outbox("s2") == []  # another conversation never sees them
    files.make_document("s1", "T", [], ["md", "tex"], "", "Mon Fichier!")
    assert [n for n, _, _ in outbox("s1")][-2:] == ["mon-fichier.md", "mon-fichier.tex"]


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("formats_", "message"),
    [([], "parmi"), (["rtf"], "pas « rtf »"), ("docx", "parmi"), (["pdf", "exe"], "pas « exe »")],
)
def test_a_wrong_format_or_content_keeps_nothing(db_cabinet, formats_, message):
    with pytest.raises(ToolError, match=message):
        files.make_document("s1", "T", BLOCKS, formats_)
    with pytest.raises(ToolError, match="Bloc 1"):
        files.make_document("s1", "T", [{"type": "x"}], ["docx", "pdf"])
    with pytest.raises(ToolError, match="conversation"):
        files.make_document("", "T", BLOCKS, ["docx"])
    assert outbox("s1") == []


@pytest.mark.technical
def test_a_file_over_the_limit_is_refused_before_any_is_kept(db_cabinet, monkeypatch):
    monkeypatch.setattr(files, "MAX_BYTES", 2000)
    with pytest.raises(ToolError, match="dépasse"):
        files.make_document("s1", "T", BLOCKS, ["md", "docx"])
    assert outbox("s1") == []  # the small markdown was made first, and not kept


@pytest.mark.communication
def test_the_tool_tells_what_the_reader_should_know(db_cabinet):
    text = files.make_document("s1", "T", [{"type": "paragraph", "text": "عربى"}], ["pdf", "tex"])
    assert "hors du jeu Latin" in text and "à compiler" in text


@pytest.mark.functionality
def test_what_the_model_may_send_wrongly_is_a_clear_error_never_a_crash():
    for blocks, message in [
        ([{"type": []}], "Bloc 1"),
        ([{"type": {}}], "Bloc 1"),
        ([None], "Bloc 1"),
        ([{"type": "heading", "level": 2.0, "text": "x"}], "niveau"),
        ([{"type": "heading", "level": True, "text": "x"}], "niveau"),
        ([{"type": "heading", "level": "2", "text": "x"}], "niveau"),
        ([{"type": "paragraph", "text": 10**5000}], "trop grand"),
        ([{"type": "paragraph", "text": float("nan")}], "trop grand"),
    ]:
        with pytest.raises(ToolError, match=message):
            document.parse("T", blocks)
    assert document.parse("T", [{"type": "paragraph", "text": "a￾b﷐c‮d​e﻿"}]).blocks[0]["text"] == "abcde"
    for render in formats.RENDERERS.values():  # the characters XML refuses never reach Word or Excel
        render(document.parse("T￾", [{"type": "paragraph", "text": "x￿y"}]))


@pytest.mark.security
def test_text_that_reverses_or_hides_is_stripped_so_a_figure_reads_as_stored():
    doc = made([{"type": "table", "headers": ["Libellé"], "rows": [["facture‮txt.exe"], ["12​3"]]}])
    assert doc.blocks[0]["rows"] == [["facturetxt.exe"], ["123"]]


@pytest.mark.functionality
def test_a_pdf_table_too_dense_for_a_page_is_refused_in_french():
    row = ["x" * 500] * 20
    doc = made([{"type": "table", "headers": [f"c{n}" for n in range(20)], "rows": [row]}])
    with pytest.raises(ToolError, match="trop dense pour le PDF"):
        formats.render_pdf(doc)
    assert formats.render_docx(doc)[0] and formats.render_xlsx(doc)[0]  # the other formats take it


@pytest.mark.functionality
def test_only_ascii_figures_of_at_most_eleven_digits_become_numbers():
    assert [
        formats.number_of(t) for t in ["١٢٣", "12345678901234", "123456789012", "12345678901", "1,234", "007", "-0,5"]
    ] == [
        None,
        None,
        None,
        12345678901,
        1.234,
        None,
        -0.5,
    ]
    sheet = openpyxl.load_workbook(
        io.BytesIO(
            formats.render_xlsx(made([{"type": "table", "headers": ["a"], "rows": [["١٢٣"], ["12345678901234"]]}]))[0]
        )
    )["Tableau"]
    assert sheet["A2"].value == "١٢٣" and sheet["A3"].value == "12345678901234" and sheet["A3"].data_type == "s"


@pytest.mark.functionality
def test_excel_sheet_names_avoid_what_makes_excel_ask_to_repair():
    taken = []
    names = [formats.sheet_name(wanted, taken) for wanted in ["'Hist'", "History", "history", "a" * 40, "[:]"]]
    assert names[0] == "Hist" and names[1] == "Tableau"[:0] + names[1] and names[1].lower() != "history"
    assert all(len(n) <= 31 and not n.startswith("'") and not n.endswith("'") for n in names)
    assert formats.sheet_name("x", ["x"]) == "x 2"


@pytest.mark.security
def test_in_latex_a_bracket_opening_an_item_or_a_row_is_no_option_and_in_markdown_no_link_or_tag_survives():
    doc = made(
        [
            {"type": "bullets", "items": ["[ok] x"]},
            {"type": "table", "headers": ["a"], "rows": [["1"], ["[2] z"]]},
            {"type": "heading", "level": 1, "text": "Titre\n# injecté"},
            {"type": "paragraph", "text": "[Voir](http://evil.example) <img src=x>"},
        ],
        title="T\n## faux",
    )
    body = formats.render_tex(doc)[0].decode()
    assert r"\item {}[ok] x" in body and "{} [2] z" in body
    md = formats.render_md(doc)[0].decode()
    assert md.splitlines()[0] == "# T ## faux" and "## Titre # injecté" in md
    assert not re.search(r"(?<!\\)\[[^\]]*\]\(", md) and "<img" not in md  # an escaped bracket makes no link
    assert r"\[Voir]" in md and "&lt;img" in md


@pytest.mark.technical
def test_the_table_cell_limit_keeps_pdf_and_word_to_a_few_seconds():
    with pytest.raises(ToolError, match="10000"):
        made([{"type": "table", "headers": list("abcdef"), "rows": [["x"] * 6] * 2000}])


@pytest.mark.security
def test_a_conversations_outbox_cannot_fill_up_with_documents_nobody_has_sent(db_cabinet):
    for n in range(5):
        files.make_document("s1", f"R{n}", BLOCKS, ["md", "docx", "pdf"])  # 3 files each: 15 in all
    assert len(outbox("s1")) == 15
    with pytest.raises(ToolError, match="Trop de fichiers"):
        files.make_document("s1", "R9", BLOCKS, ["md"])
    assert len(outbox("s1")) == 15 and files.make_document("s2", "R", BLOCKS, ["md"])  # another conversation is free
