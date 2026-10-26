"""create_import_folder against Experio: its page types, the folder made, test mode, refusals and questions."""

import io

import pypdfium2
import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.invoices import import_folder

TYPES = [["transit", "Transit"], ["customs", "Douane"], ["dum", "DUM"], ["other", "Autre"]]


def pdf(pages):
    document = pypdfium2.PdfDocument.new()
    for _ in range(pages):
        document.new_page(595, 842)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def saved(name, data, session="imp", mime="application/pdf"):
    with cabinet_db() as db:
        return db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data) values (%s, %s, %s, %s) returning id",
            (session, name, mime, data),
        ).fetchone()[0]


class Experio:
    def __init__(self, rule="file_name", refusal=None):
        self.calls, self.json, self.rule, self.refusal = [], [], rule, refusal
        self.ids, self.folders = iter(range(101, 120)), iter(range(9, 20))

    def call(self, model, method, *args, **kw):
        self.calls.append((model, method))
        if method == "fields_get":
            if self.refusal:
                raise self.refusal
            return {"import_invoice_type": {"selection": TYPES}}
        if model == "res.company":
            return [{"import_folder_number": self.rule}]
        if model == "exp.invoice.import":
            return 77
        if model == "exp.invoice":
            return [{"id": i, "import_invoice_type": "transit" if i == 101 else "customs"} for i in args[0]]
        raise AssertionError(f"unexpected {model}.{method}")

    def post(self, model, method, *args, **kw):
        self.json.append((model, method, args))
        if method == "action_add_documents_from_javascript":
            return [next(self.ids)]
        if method == "create_folder_from_js":
            return next(self.folders)
        return True


@pytest.fixture
def experio(db_cabinet, monkeypatch):
    fake = Experio()
    pages = iter([("transit", True), ("customs", True), ("transit", True), ("customs", True)])
    monkeypatch.setattr(import_folder, "odoo_call", fake.call)
    monkeypatch.setattr(import_folder, "odoo_json", fake.post)
    monkeypatch.setattr(import_folder, "require_dossier", lambda dossier_id: "ATLAS")
    monkeypatch.setattr(import_folder, "dry_run", lambda: False)
    monkeypatch.setattr(import_folder, "typed_page", lambda image, kinds, dossier, what: next(pages))
    return fake


def made_before():
    """A file already made into folder 1425: its content."""
    data = pdf(1)
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind, folder_id) "
            "values ('old', 'imp.pdf', 'application/pdf', %s, 'folder', 1425)",
            (data,),
        )
    return data


def folder_of(file_id):
    with cabinet_db() as db:
        return db.execute("select kind, folder_id from tarik_files where id = %s", (file_id,)).fetchone()


@pytest.mark.functionality
def test_experio_page_types_are_read_from_its_own_screen(monkeypatch):
    asked = []

    def odoo_call(model, method, fields, attributes):
        asked.append((model, method, fields, attributes))
        return {"import_invoice_type": {"selection": TYPES}}

    monkeypatch.setattr(import_folder, "odoo_call", odoo_call)
    assert import_folder.page_types()["customs"] == "Douane"
    assert asked == [("exp.folder.import", "fields_get", ["import_invoice_type"], ["selection"])]


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("answer", "typed"),
    [
        ({"type": "customs", "starts": False}, ("customs", False)),
        ({"type": "invented"}, ("other", True)),  # never a type Experio doesn't have; a start unless said otherwise
    ],
)
def test_a_page_is_typed_among_experio_types_only(monkeypatch, answer, typed):
    monkeypatch.setattr(import_folder, "tool_prompt", lambda name: "prompt")
    monkeypatch.setattr(import_folder, "ask_model", lambda system, pages, instruction, what, think: answer)
    assert import_folder.typed_page(b"png", dict(TYPES), "ATLAS", "page 1") == typed


@pytest.mark.functionality
def test_the_files_become_one_folder_its_drafts_kept_the_transit_invoice_last(experio):
    first, second = saved("imp mars.pdf", pdf(2)), saved("dum.pdf", pdf(1))
    answer = import_folder.create_import_folder("imp", 857, "Voici le dossier d'import")
    assert answer["status"] == "ok" and answer["invoices"] == [102, 103, 101]
    assert "dossier d'import n° 9 créé dans le dossier ATLAS : Transit, Douane, Transit" in answer["message"]
    (values,) = next(args for _, method, args in experio.json if method == "create_folder_from_js")
    assert values["folder_number"] == "imp mars" and values["invoice_ids"] == [[6, 0, [101, 102, 103]]]
    assert len(pypdfium2.PdfDocument(import_folder.base64.b64decode(values["document_pdf"]))) == 3
    assert folder_of(first) == ("folder", 9) and folder_of(second) == ("folder", 9)
    with cabinet_db() as db:
        parts = db.execute("select parent_id, invoice_id from tarik_files where parent_id is not null order by id")
        assert parts.fetchall() == [(first, 101), (first, 102), (second, 103)]


@pytest.mark.functionality
def test_several_folders_one_per_file_on_the_client_word(experio):
    first, second = saved("a.pdf", pdf(1)), saved("b.pdf", pdf(1))
    answer = import_folder.create_import_folder("imp", 857, "plusieurs dossiers d'import, un dossier par fichier")
    assert "n° 9" in answer["message"] and "n° 10" in answer["message"]
    assert folder_of(first) == ("folder", 9) and folder_of(second) == ("folder", 10)


@pytest.mark.security
def test_test_mode_makes_no_import_folder(experio, monkeypatch):
    monkeypatch.setattr(import_folder, "dry_run", lambda: True)
    file_id = saved("imp.pdf", pdf(2))
    answer = import_folder.create_import_folder("imp", 857, "dossier d'import")
    assert answer["message"].startswith("MODE TEST") and "2 pièce(s) : Transit, Douane" in answer["message"]
    assert answer["invoices"] == [] and experio.json == [] and ("exp.invoice.import", "create") not in experio.calls
    assert folder_of(file_id) == (None, None)


@pytest.mark.communication
def test_a_file_neither_pdf_nor_photo_is_refused(experio):
    saved("liste.xlsx", b"PK", mime="application/vnd.ms-excel")
    with pytest.raises(ToolError, match="format non pris en charge"):
        import_folder.create_import_folder("imp", 857)


@pytest.mark.communication
def test_no_file_asks_for_the_folder_documents(experio):
    answer = import_folder.create_import_folder("empty", 857)
    assert answer["status"] == "question" and "joignez ses pièces" in answer["message"]


@pytest.mark.functionality
def test_a_folder_already_made_from_the_same_file_stops(experio, monkeypatch):
    data = made_before()
    saved("imp (1).pdf", data)
    monkeypatch.setattr(import_folder, "odoo_call", lambda model, method, domain: [1425])
    answer = import_folder.create_import_folder("imp", 857)
    assert answer["status"] == "stop" and answer["duplicate"] and "dossier n° 1425" in answer["message"]


@pytest.mark.functionality
def test_a_folder_gone_from_experio_is_made_again(db_cabinet, monkeypatch):
    data = made_before()
    asked = []
    monkeypatch.setattr(import_folder, "odoo_call", lambda model, method, domain: asked.append(domain) or [])
    assert import_folder.folder_twins(857, [(saved("imp (1).pdf", data), "imp (1).pdf", data)]) == []
    assert asked == [[("id", "=", 1425), ("company_id", "=", 857)]]


@pytest.mark.communication
def test_experio_refusing_the_folder_says_so(experio):
    experio.refusal = ConnectionRefusedError("down")
    saved("imp.pdf", pdf(1))
    with pytest.raises(ToolError, match="n'a pas pu créer le dossier d'import"):
        import_folder.create_import_folder("imp", 857)


@pytest.mark.functionality
def test_no_draft_reads_no_invoice(monkeypatch):
    monkeypatch.setattr(import_folder, "odoo_call", lambda *a, **k: pytest.fail("nothing to read"))
    assert import_folder.folder_invoices([]) == []
