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
    def __init__(self, rule="file_name", refusal=None, rate="bank_al_maghrib"):
        self.calls, self.json, self.rule, self.refusal, self.rate = [], [], rule, refusal, rate
        self.ids, self.folders = iter(range(101, 120)), iter(range(9, 20))
        self.dum, self.written = {}, []  # what Tarik reads on a DUM (dum_values), what it writes in Experio
        self.customs = {"customs_invoice_number": "num_liquidation", "customs_date": "date_liquidation"}

    def call(self, model, method, *args, **kw):
        self.calls.append((model, method))
        if method == "fields_get":
            if self.refusal:
                raise self.refusal
            return {"import_invoice_type": {"selection": TYPES}}
        if model == "res.company":
            return [{"import_folder_number": self.rule, "exchange_rate": self.rate, **self.customs}]
        if model == "exp.invoice.import":
            return 77
        if model == "exp.invoice" and method == "write":
            self.written.append(("customs", args[0], args[1]))
            return True
        if model == "exp.invoice" and ("import_invoice_type", "=", "customs") in args[0]:
            return [i for i in args[0][0][2] if i == 102]  # the customs one
        if model == "exp.invoice":
            assert method == "search" and args[0][1] == import_folder.TRANSIT
            return [i for i in args[0][0][2] if i == 101]  # the transit agent's
        if model == "exp.import.folder" and method == "write":
            self.written.append((args[0], args[1]))
            return True
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
    monkeypatch.setattr(import_folder, "dum_values", lambda pdf, dossier, name: dict(fake.dum))
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

    def odoo_call(model, method, fields, attributes, context):
        asked.append((model, method, fields, attributes, context))
        return {"import_invoice_type": {"selection": TYPES}}

    monkeypatch.setattr(import_folder, "odoo_call", odoo_call)
    assert import_folder.page_types()["customs"] == "Douane"
    # in French: the labels are told to the client, whatever language Tarik's Experio user reads
    assert asked == [("exp.folder.import", "fields_get", ["import_invoice_type"], ["selection"], {"lang": "fr_FR"})]


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
    assert "dossier d'import n° 9 créé dans le dossier `ATLAS` : Transit, Douane, Transit" in answer["message"]
    (values,) = next(args for _, method, args in experio.json if method == "create_folder_from_js")
    assert values["folder_number"] == "imp mars" and values["invoice_ids"] == [[6, 0, [101, 102, 103]]]
    assert len(pypdfium2.PdfDocument(import_folder.base64.b64decode(values["document_pdf"]))) == 3
    assert folder_of(first) == ("folder", 9) and folder_of(second) == ("folder", 9)
    with cabinet_db() as db:
        parts = db.execute("select parent_id, invoice_id from tarik_files where parent_id is not null order by id")
        assert parts.fetchall() == [(first, 101), (first, 102), (second, 103)]


@pytest.mark.communication
def test_the_dum_tarik_reads_sets_the_folders_rate_and_an_unread_one_leaves_bank_al_maghribs(experio, monkeypatch):
    pages = iter([("transit", True), ("customs", True), ("dum", True)] * 3)
    monkeypatch.setattr(import_folder, "typed_page", lambda image, kinds, dossier, what: next(pages))
    experio.dum = {"rate": 10.8523, "date": "2026-03-02", "number": "301-77"}
    experio.customs = {"customs_invoice_number": "num_dum", "customs_date": "date_liquidation"}
    saved("imp mars.pdf", pdf(2)), saved("dum.pdf", pdf(1))
    experio.rate = "dum"
    message = import_folder.create_import_folder("imp", 857, "Voici le dossier d'import")["message"]
    assert "taux de change de la DUM lu et écrit sur le dossier : 10.8523" in message
    assert experio.written == [
        ([9], {"exchange_rate_dum": 10.8523, "date_dum": "2026-03-02"}),
        ("customs", [102], {"invoice_number": "301-77"}),  # the dossier numbers its customs invoices by DUM
    ]
    assert "Banque Al Maghrib" not in message  # every folder has its DUM's rate
    experio.dum, experio.written = {"date": "2026-04-01"}, []  # a DUM whose rate could not be read
    saved("imp avril.pdf", pdf(2), session="imp2"), saved("dum avril.pdf", pdf(1), session="imp2")
    message = import_folder.create_import_folder("imp2", 857, "plusieurs dossiers d'import")["message"]
    assert message.count("je n'ai pas pu lire son taux de change") == 1  # the folder with the DUM
    error = "Le système n'a pas pu détecter le taux de change depuis la DUM."  # flagged as Experio's reading does
    assert experio.written[0][1] == {"date_dum": "2026-04-01", "error_msg": error}
    assert message.count("le dossier prend ses taux de change de la DUM") == 1  # once, whatever the folders
    assert "« Banque Al Maghrib », et celles comptabilisées avant le gardent" in message


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("answer", "read"),
    [
        (
            {"exchange_rate": 10.85, "date": "2026-03-02", "amount_declared": 1200.5, "number": " 301-2026-77 "},
            {"rate": 10.85, "date": "2026-03-02", "amount": 1200.5, "number": "301-2026-77"},
        ),
        ({"exchange_rate": None, "date": "02/03/2026", "number": "301"}, {"number": "301"}),  # only what is clear
        ({"exchange_rate": "10,85", "amount_declared": True}, {}),  # never a figure not given as a number
    ],
)
def test_a_dum_gives_only_what_the_model_read_clearly(monkeypatch, answer, read):
    monkeypatch.setattr(import_folder, "tool_prompt", lambda name: "prompt")
    monkeypatch.setattr(import_folder, "pdf_pages", lambda raw, limit: [b"png"])
    monkeypatch.setattr(import_folder, "ask_model", lambda system, pages, instruction, what, think: answer)
    assert import_folder.dum_values(b"%PDF", "ATLAS", "dum.pdf") == read


@pytest.mark.functionality
def test_a_dum_numbers_the_folder_and_dates_its_customs_invoices_as_the_dossier_says(experio):
    rule = {"import_folder_number": "dum_invoice", "exchange_rate": "bank_al_maghrib"}
    rule.update(customs_invoice_number="num_liquidation", customs_date="date_dum")
    read = {"rate": 10.85, "amount": 1200.5, "number": "301-77", "date": "2026-03-02"}
    assert import_folder.write_dum(9, [101, 102], read, rule, {}) == 10.85
    folder = {"exchange_rate_dum": 10.85, "amount_dum_declared": 1200.5, "date_dum": "2026-03-02"}
    assert experio.written == [
        ([9], {**folder, "folder_number": "301-77"}),
        ("customs", [102], {"invoice_date": "2026-03-02"}),
    ]


@pytest.mark.functionality
def test_a_dum_the_model_cannot_read_is_left_to_the_cabinet(monkeypatch):
    def failing(*a, **k):
        raise ToolError("le modèle ne répond pas")

    monkeypatch.setattr(import_folder, "tool_prompt", lambda name: "prompt")
    monkeypatch.setattr(import_folder, "pdf_pages", lambda raw, limit: [b"png"])
    monkeypatch.setattr(import_folder, "ask_model", failing)
    assert import_folder.dum_values(b"%PDF", "ATLAS", "dum.pdf") == {}


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("files", "first_pages", "found", "calls"),
    [
        (["a.pdf", "b.pdf"], ["supplier", "dum"], True, 2),  # a DUM among them
        (["a.pdf", "b.pdf", "c.pdf"], ["transit", "customs", "other"], True, 2),  # stops once found
        (["a.pdf", "b.pdf"], ["transit", "supplier"], False, 2),  # a transit agent's invoice alone: not enough
        (["a.pdf"], ["dum"], False, 0),  # one file is never a folder unnamed: not even looked at
    ],
)
def test_new_files_looking_like_an_import_folder_are_found(experio, monkeypatch, files, first_pages, found, calls):
    typed = iter(first_pages)
    asked = []
    monkeypatch.setattr(import_folder, "pdf_pages", lambda raw, limit: [b"png"])

    def typed_page(image, kinds, dossier, what):
        asked.append(what)
        return next(typed), True

    monkeypatch.setattr(import_folder, "typed_page", typed_page)
    new = [saved(name, pdf(1)) for name in files]
    assert import_folder.looks_like_folder("imp", 857, new) is found
    assert len(asked) == calls  # one quick look at each file's first page, at most


@pytest.mark.functionality
def test_only_files_just_sent_and_never_beside_another_kind_are_looked_at(experio, monkeypatch):
    monkeypatch.setattr(import_folder, "typed_page", lambda *a: pytest.fail("no model call"))
    earlier = [saved("dum.pdf", pdf(1)), saved("transit.pdf", pdf(1))]
    assert not import_folder.looks_like_folder("imp", 857, [])  # files from an earlier message: not again
    new = [saved("c.pdf", pdf(1)), saved("d.pdf", pdf(1))]
    saved("releve.xlsx", b"x", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert not import_folder.looks_like_folder("imp", 857, [*earlier, *new])  # a folder would refuse the sheet


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
    assert answer["message"].startswith("MODE TEST") and "2 pièces : Transit, Douane" in answer["message"]
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
