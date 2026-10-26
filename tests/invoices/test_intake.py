"""What the client sends becomes a draft: a dossier first, the file identified, invoices cut apart."""

import asyncio
import io

import pypdfium2
import pytest
from PIL import Image

from engine.calls import ToolFailure, call
from engine.tools.core.app import ToolError
from engine.tools.core.queries import Q
from engine.tools.invoices.intake import require_kind, split_pdf
from tests.fakes import FakeDb


@pytest.mark.security
@pytest.mark.parametrize("args", [{"file_id": 1}, {"file_id": 1, "dossier_id": None}, {"file_id": 1, "dossier_id": 0}])
def test_a_draft_needs_a_dossier_before_anything_is_touched(args, db_cabinet):
    with pytest.raises(ToolFailure, match="dossier|informations manquent"):  # refused, before anything is touched
        asyncio.run(call(db_cabinet, "create_draft_invoice", **args))


@pytest.mark.technical
def test_a_pdf_is_cut_where_its_invoices_start():
    buffer = io.BytesIO()
    pages = [Image.new("RGB", (200, 280), color) for color in ("white", "gray", "white", "gray")]
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    four = buffer.getvalue()
    parts = split_pdf(four, [1, 3])
    assert [(first, last) for first, last, _ in parts] == [(1, 2), (3, 4)]
    assert [len(pypdfium2.PdfDocument(raw)) for _, _, raw in parts] == [2, 2]
    assert [(first, last) for first, last, _ in split_pdf(four, [3, 0, 9, 3])] == [
        (1, 2),
        (3, 4),
    ]  # page 1 added, out of range dropped
    assert [(first, last) for first, last, _ in split_pdf(four, [])] == [(1, 4)]
    assert len(split_pdf(four, [1, 2, 3, 4])) == 4


@pytest.mark.functionality
def test_a_file_is_identified_before_anything_is_made_from_it():
    require_kind("f.pdf", "invoice", "invoice")
    for kind, wanted, hint in (
        (None, "invoice", "identify_document"),
        ("statement", "invoice", "import_bank_statement"),
        ("invoice", "statement", "split_invoices"),
    ):
        with pytest.raises(ToolError, match=hint):
            require_kind("f.pdf", kind, wanted)


@pytest.mark.security
def test_test_mode_creates_no_draft_and_imports_no_statement(monkeypatch):
    from engine.tools.invoices import intake

    def odoo_call(model, method, *args, **kw):
        if method == "search_read":  # the dossier's bank journals
            return [{"id": 5, "name": "BMCE", "bank_id": [1, "BMCE"]}]
        raise AssertionError(f"test mode wrote to Experio: {model}.{method}")

    monkeypatch.setattr(intake, "odoo_call", odoo_call)
    monkeypatch.setattr(intake, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(intake, "dry_run", lambda: True)

    monkeypatch.setattr(
        intake, "cabinet_db", lambda: FakeDb(("s", "f.pdf", "application/pdf", b"%PDF", "invoice", None, None, None))
    )
    draft = intake.create_draft_invoice(1, 557)
    assert draft["status"] == "stop" and draft["message"].startswith("MODE TEST") and "invoice_id" not in draft

    class FileOnly(FakeDb):  # the file's row; no other file of the same content
        def fetchone(self):
            return self.row if self.queries[-1][0] == Q.file_locked else None

    monkeypatch.setattr(
        intake,
        "cabinet_db",
        lambda: FileOnly(("s", "r.pdf", "application/pdf", b"%PDF", "statement", None, None, None)),
    )
    statement = intake.import_bank_statement(2, 557)
    assert (
        statement["status"] == "ok" and statement["message"].startswith("MODE TEST") and "BMCE" in statement["message"]
    )


def pdf(count):
    """A PDF of ``count`` blank pages."""
    buffer = io.BytesIO()
    pages = [Image.new("RGB", (200, 280), "white") for _ in range(count)]
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


@pytest.mark.functionality
@pytest.mark.parametrize(
    "said, choice",
    [
        ("Une facture par page svp", "by_page"),
        ("Découpez page par page", "by_page"),
        ("par document", "by_document"),
        ("C'est une seule facture", "by_document"),
        ("Voici les factures d'achat", None),  # nothing said: asked
        ("par page ou par document, je ne sais pas", None),  # both said: never guessed
    ],
)
def test_how_a_pdf_is_cut_is_the_client_s_word(said, choice):
    from engine.tools.invoices.intake import split_choice

    assert split_choice(["Voici les factures d'achat de TARIK TEST", said]) == choice


def saved(db_cabinet, data, session="s-split"):
    from engine.tools.core.cabinets import cabinet_db

    with cabinet_db() as db:
        (file_id,) = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind) "
            "values (%s, 'lot.pdf', 'application/pdf', %s, 'invoice') returning id",
            (session, data),
        ).fetchone()
    return file_id


@pytest.mark.functionality
def test_a_pdf_of_several_pages_waits_for_the_client_to_say_how_to_cut_it(db_cabinet):
    from engine.tools.invoices.intake import split_invoices

    file_id = saved(db_cabinet, pdf(3))
    asked = split_invoices(file_id=file_id, invoice_type="achat", client_message="Voici les factures d'achat")
    assert asked["status"] == "question" and "par page" in asked["message"] and "par document" in asked["message"]
    whole = split_invoices(file_id=file_id, invoice_type="achat", client_message="par document, achat")
    assert whole["status"] == "ok" and whole["files"] == [file_id]


@pytest.mark.functionality
def test_a_pdf_cut_per_page_is_one_invoice_per_page_and_one_page_never_asked(db_cabinet):
    from engine.tools.invoices.intake import split_invoices

    file_id = saved(db_cabinet, pdf(3), "s-pages")
    cut = split_invoices(file_id=file_id, invoice_type="achat", client_message="achat, une facture par page")
    assert cut["status"] == "ok" and len(cut["files"]) == 3 and file_id not in cut["files"]
    one = saved(db_cabinet, pdf(1), "s-one")
    assert split_invoices(file_id=one, invoice_type="achat", client_message="achat")["files"] == [one]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "callback_url, gladden_says, kind, model_asked",
    [
        ("https://tarik.example/gladden", "bank", "statement", True),  # checked by the model, which has the last word
        ("https://tarik.example/gladden", "invoice", "invoice", False),
        ("https://tarik.example/gladden", None, "statement", True),  # no answer: the model decides
        ("https://tarik.example/gladden", "receipt", "statement", True),  # a type Tarik doesn't take: the model
        ("", "invoice", "statement", True),  # Gladden not set up: never sent
    ],
)
def test_gladden_identifies_a_file_first_and_the_model_when_it_cant(
    callback_url, gladden_says, kind, model_asked, monkeypatch
):
    from engine.tools.invoices import intake

    sent, asked = [], []
    db = FakeDb((None, "f.pdf", "application/pdf", pdf(3), None, None, None, None))
    monkeypatch.setattr(intake, "cabinet_db", lambda: db)
    settings = {"GLADDEN_URL": "https://runner.example/gladden", "GLADDEN_CALLBACK_URL": callback_url}
    monkeypatch.setattr(intake, "setting", lambda key, default=None: settings[key])

    def classify(url, raw, callback):
        sent.append(len(pypdfium2.PdfDocument(raw)))
        return gladden_says

    monkeypatch.setattr(intake.gladden, "classify", classify)
    monkeypatch.setattr(intake, "classify_pages", lambda pages, what: asked.append(len(pages)) or ("rb", ""))
    answer = intake.identify_document(1)
    assert answer["kind"] == kind and bool(asked) == model_asked
    assert sent == ([1] if callback_url else [])  # the first page only, as the model sees it
    assert db.queries[-1][1] == (kind, 1)


@pytest.mark.security
@pytest.mark.parametrize(
    "said, kind",
    [
        ("Ce n'est pas un relevé bancaire, c'est une facture d'achat.", "invoice"),
        ("facture.pdf : ce n'est pas un relevé", "invoice"),
        ("Il s'agit d'une facture de douane", "invoice"),
        ("c'est un relevé, pas une facture", "statement"),
        ("Voici la facture et le relevé de mars", None),  # what was sent, not what a file is
        ("c'est une facture ; c'est un relevé", None),  # both: nothing decided
        ("", None),
    ],
)
def test_what_the_client_says_a_file_is_is_read_from_their_words_only(said, kind):
    from engine.tools.invoices.intake import client_kind

    assert client_kind(said) == kind


@pytest.mark.security
@pytest.mark.parametrize(
    "message, imported, kind",
    [
        ("Objet : Re: relevé\n\ndouane.pdf n'est pas un relevé, c'est une facture d'achat.", None, "invoice"),
        ("Objet : Re: relevé\n\nce n'est pas un relevé, c'est une facture.", None, "statement"),  # which file?
        ("Objet : Re: relevé\n\ndouane.pdf n'est pas un relevé, c'est une facture.", 4524, "statement"),  # imported
    ],
)
def test_the_client_corrects_a_file_s_kind_until_it_is_imported(message, imported, kind, monkeypatch):
    from engine.tools.invoices import intake

    db = FakeDb(("s", "douane.pdf", "application/pdf", pdf(1), "statement", None, imported, None))
    monkeypatch.setattr(intake, "cabinet_db", lambda: db)
    monkeypatch.setattr(intake, "gladden_type", lambda raw: pytest.fail("a known kind is never read again"))
    assert intake.identify_document(1, client_message=message)["kind"] == kind
    assert any(params == ("invoice", 1) for _, params in db.queries) == (kind == "invoice")


@pytest.mark.functionality
def test_a_document_that_is_neither_invoice_nor_statement_is_said_and_left_alone(monkeypatch):
    from engine.tools.invoices import intake

    db = FakeDb((None, "contrat leasing.pdf", "application/pdf", pdf(2), None, None, None, None))
    monkeypatch.setattr(intake, "cabinet_db", lambda: db)
    monkeypatch.setattr(intake, "gladden_type", lambda raw: None)
    monkeypatch.setattr(intake, "classify_pages", lambda pages, what: ("autre", "un contrat de crédit-bail"))
    answer = intake.identify_document(1)
    assert answer["status"] == "stop" and answer["kind"] == "other"
    assert answer["message"].startswith("`contrat leasing.pdf` : ni une facture ni un relevé bancaire")
    assert db.queries[-1][1] == ("other", 1)


@pytest.mark.functionality
def test_a_statement_sent_again_is_not_imported_twice_in_its_dossier(db_cabinet, monkeypatch):
    from engine.tools.core.cabinets import cabinet_db
    from engine.tools.invoices import intake

    with cabinet_db() as db:
        insert = (
            "insert into tarik_files (session_id, file_name, mime_type, data, kind, statement_id) "
            "values (%s, %s, 'application/pdf', %s, 'statement', %s) returning id"
        )
        first = db.execute(insert, ("s1", "releve.pdf", b"%PDF same", 42)).fetchone()[0]
        again = db.execute(insert, ("s2", "releve (1).pdf", b"%PDF same", None)).fetchone()[0]
        other = db.execute(insert, ("s2", "avril.pdf", b"%PDF other", None)).fetchone()[0]
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, dossier_id, step, status, message) "
            "values ('r', 's1', %s, 557, 'import_bank_statement', 'ok', 'importé')",
            (first,),
        )
    monkeypatch.setattr(intake, "require_dossier", lambda dossier_id: "D")
    monkeypatch.setattr(intake, "statement_journal", lambda *a: {"status": "question", "message": "lequel ?"})
    answer = intake.import_bank_statement(again, 557)
    assert answer["statement_id"] == 42 and "déjà importé (n° 42)" in answer["message"]
    with cabinet_db() as db:
        assert db.execute("select statement_id from tarik_files where id = %s", (again,)).fetchone() == (42,)
    assert intake.import_bank_statement(other, 557)["status"] == "question"  # another statement: imported as usual
    assert intake.import_bank_statement(again, 558)["statement_id"] == 42  # linked to it now: never imported again


@pytest.mark.functionality
def test_a_leasing_schedule_gladden_calls_a_statement_is_set_aside_by_the_model(monkeypatch):
    from engine.tools.invoices import intake

    db = FakeDb((None, "echeancier.pdf", "application/pdf", pdf(1), None, None, None, None))
    monkeypatch.setattr(intake, "cabinet_db", lambda: db)
    monkeypatch.setattr(intake, "gladden_type", lambda raw: "bank")
    monkeypatch.setattr(intake, "classify_pages", lambda pages, what: ("autre", "un échéancier de leasing"))
    answer = intake.identify_document(1)
    assert answer["kind"] == "other" and answer["status"] == "stop"


@pytest.mark.functionality
@pytest.mark.parametrize(
    "said, journal",
    [
        ("Bonjour, relevé du compte BMCE.", 3245),
        ("le relevé Bank of Africa de juillet", 3245),
        ("relevé Crédit du Maroc", 3246),
        ("relevé SGMB", 3244),
        ("Voici le relevé.", None),  # no bank named: asked
        ("relevés BMCE et SGMB", None),  # two: asked
    ],
)
def test_the_bank_account_the_client_names_takes_the_statement(said, journal, monkeypatch):
    from engine.tools.invoices import intake

    usable = [
        {"id": 3245, "name": "BMCE", "bank_id": [1, "BANK OF AFRICA (BMCE)"]},
        {"id": 3246, "name": "CREDIT DU MAROC", "bank_id": [2, "CREDIT DU MAROC"]},
        {"id": 3244, "name": "SGMB", "bank_id": [3, "SAHAM BANK"]},
    ]
    monkeypatch.setattr(intake, "bank_journals", lambda dossier_id: (usable, usable))
    found = intake.statement_journal("r.pdf", "D", 1, 0, f"Objet : Relevé\n\n{said}")
    assert (found.get("id") if "status" not in found else None) == journal


@pytest.mark.functionality
def test_a_screenshot_gladden_calls_an_invoice_never_becomes_a_draft(monkeypatch):
    from engine.tools.invoices import intake

    buffer = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(buffer, format="PNG")
    db = FakeDb((None, "capture.png", "image/png", buffer.getvalue(), None, None, None, None))
    monkeypatch.setattr(intake, "cabinet_db", lambda: db)
    monkeypatch.setattr(intake, "gladden_type", lambda raw: "invoice")
    monkeypatch.setattr(intake, "classify_pages", lambda pages, what: ("autre", "une capture d'écran d'un logiciel"))
    assert intake.identify_document(1)["kind"] == "other"


@pytest.mark.security
@pytest.mark.parametrize(
    "name, said, named",
    [
        ("douane.pdf", "douane n'est pas un relevé", True),
        ("facture.pdf", "la facture n'est pas un relevé", False),  # a common word: names no file
        ("facture.pdf", "facture.pdf n'est pas un relevé", True),  # written whole, with its extension
        ("lot.zip › releve.pdf", "la facture n'est pas un relevé", False),
        ("scan 12.pdf", "le scan 12 est une facture", False),
        ("Mars MIDAS.pdf", "mars midas n'est pas un relevé", True),
    ],
)
def test_a_file_is_named_by_its_own_words_never_by_a_common_one(name, said, named):
    from engine.tools.invoices.intake import file_named

    assert file_named(name, said) is named


@pytest.mark.communication
@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (("achat", "Facture adressée au client du cabinet"), "achat"),
        (("vente", "Émise par le client du cabinet"), "vente"),
        (None, None),  # the scan says neither plainly: asked
    ],
)
def test_the_side_the_client_does_not_give_is_read_on_the_scan_and_said(db_cabinet, monkeypatch, read, expected):
    from engine.tools.invoices import intake

    monkeypatch.setattr(intake, "side_read", lambda name, mime, data, dossier_id: read)
    file_id = saved(db_cabinet, pdf(1), "s-side")
    result = intake.split_invoices(file_id=file_id, client_message="facture TARIK TEST", dossier_id=857)
    if expected is None:
        assert result["status"] == "question" and "achat ou de vente" in result["message"]
        return
    assert result["status"] == "ok" and not result.get("routine") and result["tell"]  # recorded and told
    assert result["invoice_type"] == expected and result["files"] == [file_id]
    assert "d'après le scan" in result["message"] and "dites-le-moi si c'est" in result["message"]


@pytest.mark.functionality
def test_the_side_the_client_gives_is_never_read_on_the_scan(db_cabinet, monkeypatch):
    from engine.tools.invoices import intake

    monkeypatch.setattr(intake, "side_read", lambda *a: pytest.fail("the client said it"))
    file_id = saved(db_cabinet, pdf(1), "s-said")
    result = intake.split_invoices(
        file_id=file_id, invoice_type="vente", client_message="facture de vente", dossier_id=857
    )
    assert result["files"] == [file_id] and "invoice_type" not in result


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("state", "working", "status", "said", "resumed"),
    [
        ("draft", False, "ok", "je la reprends", 272493),
        ("post", False, "ok", "déjà comptabilisée", None),
        ("draft", True, "stop", "en cours de traitement", None),
    ],
)
def test_a_file_sent_again_is_its_invoice_taken_up_while_a_draft(
    db_cabinet, monkeypatch, state, working, status, said, resumed
):
    from engine.tools.core.cabinets import cabinet_db
    from engine.tools.invoices import intake

    data = pdf(1)
    first = saved(db_cabinet, data, "s-first")
    with cabinet_db() as db:
        db.execute("update tarik_files set invoice_id = 272493 where id = %s", (first,))
        if working:
            db.execute("insert into tarik_working (run_id, invoice_id) values ('other', 272493)")
    monkeypatch.setattr(
        intake,
        "odoo_call",
        lambda model, method, *a, **k: (
            [272493] if method == "search" else [{"state": state, "invoice_number": "26FC04471"}]
        ),
    )
    monkeypatch.setattr(intake, "side_read", lambda *a: pytest.fail("a file sent again is never asked its side"))
    again = saved(db_cabinet, data, "s-again")
    result = intake.split_invoices(file_id=again, client_message="re facture", dossier_id=857)
    assert (
        result["status"] == status
        and said in result["message"]
        and "facture n° 272493 (26FC04471)" in result["message"]
    )
    assert result.get("resume_invoice") == resumed
    if status == "ok":
        assert result["files"] == [] and result["tell"]  # in the reply, though split_invoices' ok is quiet
