"""What intake asks of Experio: the draft and statement imports, their refusals, test mode and questions."""

import base64
import io

import openpyxl
import pypdfium2
import pytest
from PIL import Image

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import banks
from engine.tools.invoices import intake


def pdf(count):
    buffer = io.BytesIO()
    pages = [Image.new("RGB", (200, 280), "white") for _ in range(count)]
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


def canvas():
    book = openpyxl.Workbook()
    book.active.append(["Date", "Libellé", "Type d'opération", "Tiers", "Méthode de paiement", "Débit", "Crédit"])
    book.active.append(["01/08/2026", "VIREMENT", "", "", "", 0, 100])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def saved(name, data, kind, mime="application/pdf", session="s", parent_id=None):
    with cabinet_db() as db:
        return db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind, parent_id) "
            "values (%s, %s, %s, %s, %s, %s) returning id",
            (session, name, mime, data, kind, parent_id),
        ).fetchone()[0]


def row(file_id, *columns):
    with cabinet_db() as db:
        return db.execute(f"select {', '.join(columns)} from tarik_files where id = %s", (file_id,)).fetchone()


class Experio:
    """The wizards' create and read: ``made`` the records the import made."""

    def __init__(self, made=(501,), journals=None, bank_code="bmce", refusal=None):
        self.calls, self.made, self.refusal = [], list(made), refusal
        self.journals = journals if journals is not None else [{"id": 5, "name": "BMCE", "bank_id": [1, "BMCE"]}]
        self.bank_code = bank_code

    def __call__(self, model, method, *args, **kw):
        self.calls.append((model, method, args, kw))
        if model == "exp.journal":
            return self.journals
        if model == "res.bank":
            return [{"id": 1, "name": "BANK OF AFRICA (BMCE)", "code": self.bank_code}]
        if method == "create":
            if self.refusal:
                raise self.refusal
            return 77
        if method == "read":
            return [{"record_ids": self.made}]
        raise AssertionError(f"unexpected {model}.{method}")


@pytest.fixture
def experio(db_cabinet, monkeypatch):
    fake = Experio()
    monkeypatch.setattr(intake, "odoo_call", fake)
    monkeypatch.setattr(intake, "require_dossier", lambda dossier_id: "ATLAS")
    monkeypatch.setattr(intake, "dry_run", lambda: False)
    return fake


@pytest.mark.functionality
def test_a_file_that_does_not_exist_is_refused(db_cabinet):
    with pytest.raises(ToolError, match="Aucun fichier n° 999999"):
        intake.identify_document(999999)


@pytest.mark.functionality
def test_experio_excel_statement_template_is_a_statement_without_any_reading(db_cabinet, monkeypatch):
    monkeypatch.setattr(intake, "gladden_type", lambda raw: pytest.fail("never sent to Gladden"))
    file_id = saved("releve.xlsx", canvas(), None, mime="application/vnd.ms-excel")
    assert intake.identify_document(file_id)["kind"] == "statement" and row(file_id, "kind") == ("statement",)


@pytest.mark.communication
@pytest.mark.parametrize(
    ("mime", "data", "said"),
    [("text/plain", b"hello", "format non pris en charge"), ("application/pdf", b"%PDF-1.4 broken", "PDF illisible")],
)
def test_a_file_neither_pdf_nor_photo_or_unreadable_is_refused_with_why(db_cabinet, mime, data, said):
    file_id = saved("f.pdf", data, None, mime=mime)
    with pytest.raises(ToolError, match=said):
        intake.identify_document(file_id)
    assert row(file_id, "kind") == (None,)


@pytest.mark.functionality
def test_a_tpe_statement_is_a_statement_whatever_gladden_would_say(db_cabinet, monkeypatch):
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (True, "cmi"))
    monkeypatch.setattr(intake, "gladden_type", lambda raw: pytest.fail("never sent to Gladden"))
    file_id = saved("tpe.pdf", pdf(1), None)
    assert intake.identify_document(file_id)["kind"] == "statement" and row(file_id, "kind") == ("statement",)


@pytest.mark.security
def test_an_invoice_side_not_said_by_the_client_is_asked_before_cutting(db_cabinet):
    file_id = saved("lot.pdf", pdf(2), "invoice")
    answer = intake.split_invoices(file_id=file_id, client_message="Voici les factures")
    assert answer["status"] == "question" and "achat ou de vente" in answer["message"]


@pytest.mark.functionality
def test_a_file_already_cut_answers_its_parts(db_cabinet):
    file_id = saved("lot.pdf", pdf(2), "invoice")
    parts = [saved(f"lot - page {n}.pdf", pdf(1), "invoice", parent_id=file_id) for n in (1, 2)]
    answer = intake.split_invoices(file_id=file_id, invoice_type="achat", client_message="achat")
    assert answer["status"] == "ok" and answer["files"] == parts and "déjà découpé en 2" in answer["message"]


@pytest.mark.functionality
def test_an_unreadable_pdf_is_one_document(db_cabinet):
    file_id = saved("lot.pdf", b"%PDF-1.4 broken", "invoice")
    answer = intake.split_invoices(file_id=file_id, invoice_type="achat", client_message="achat")
    assert answer["files"] == [file_id] and "PDF illisible" in answer["message"]


@pytest.mark.functionality
@pytest.mark.parametrize(("invoice_type", "side"), [("vente", "sale"), ("achat", "purchase"), ("mixte", "purchase")])
def test_a_draft_is_made_by_experio_own_import_on_the_client_side(experio, invoice_type, side):
    file_id = saved("F 12.pdf", pdf(1), "invoice")
    answer = intake.create_draft_invoice(file_id, 857, invoice_type)
    assert answer["status"] == "ok" and answer["invoice_id"] == 501 and row(file_id, "invoice_id") == (501,)
    (model, method, (values,), kw), read = experio.calls
    assert (model, method, kw) == ("exp.invoice.import", "create", {"context": {"allowed_company_ids": [857]}})
    assert values["type"] == side and values["name"] == "F 12" and values["company_id"] == 857
    assert values["split_type"] == "by_document" and base64.b64decode(values["data"]).startswith(b"%PDF")
    assert read[:3] == ("exp.invoice.import", "read", ([77],))
    again = intake.create_draft_invoice(file_id, 857, invoice_type)
    assert again["status"] == "stop" and again["invoice_id"] == 501 and len(experio.calls) == 2


@pytest.mark.communication
@pytest.mark.parametrize("made", [[], [501, 502]], ids=["none", "two"])
def test_an_import_that_does_not_make_exactly_one_draft_is_refused_and_nothing_is_linked(experio, made):
    experio.made = made
    file_id = saved("F.pdf", pdf(1), "invoice")
    with pytest.raises(ToolError, match="Experio n'a pas pu créer la facture pour F.pdf"):
        intake.create_draft_invoice(file_id, 857, "achat")
    assert row(file_id, "invoice_id") == (None,)


@pytest.mark.functionality
def test_a_file_cut_in_parts_makes_no_draft_of_its_own(experio):
    file_id = saved("lot.pdf", pdf(2), "invoice")
    part = saved("lot - page 1.pdf", pdf(1), "invoice", parent_id=file_id)
    with pytest.raises(ToolError, match=f"fichiers n° {part}"):
        intake.create_draft_invoice(file_id, 857, "achat")
    assert experio.calls == []


@pytest.mark.functionality
def test_a_journal_without_bank_has_no_bank_code(monkeypatch):
    monkeypatch.setattr(intake, "odoo_call", lambda *a, **k: [])
    assert intake.bank_code({"bank_id": False}) == "" and intake.bank_code({"bank_id": [1, "X"]}) == ""


BANKS = [{"id": 1, "name": "BANK OF AFRICA (BMCE)"}, {"id": 2, "name": "CIH"}]


def fake_experio(journals, written):
    """odoo_call for statement_journal: ``journals`` the dossier's bank journals; what is written or made goes in
    ``written``."""

    def odoo_call(model, method, *args, **kw):
        if model == "res.bank":
            return BANKS
        if method in ("write", "create", "unlink"):
            written.append((model, method, *args))
            return 90 + len(written)
        if model == "exp.journal" and method == "search_read" and args[0][-1][0] == "active":  # codes, archived too
            return [{"code": "BQ"}]
        if model == "exp.journal":
            return journals
        if model == "exp.account" and method == "search_read":
            return [{"code": "51410000"}]
        if model == "exp.account" and method == "read":
            return [{"code": "51411000"}]
        if model == "exp.partner":
            return []
        if model == "res.company":
            return [{"accounts_chart_length": 8}]
        raise AssertionError((model, method))

    return odoo_call


@pytest.mark.communication
@pytest.mark.parametrize("journals", [[{"id": 5, "name": "BQ1", "bank_id": False}], []])
def test_a_dossier_without_bank_journal_asks_the_statements_bank(monkeypatch, journals):
    monkeypatch.setattr(intake, "odoo_call", fake_experio(journals, []))
    monkeypatch.setattr(intake, "dry_run", lambda: False)
    answer, _ = intake.statement_journal("r.pdf", "ATLAS", 857, 0)
    assert answer["status"] == "question" and intake.BANK_QUESTION in answer["message"]
    assert answer["choices"] == {1: "BANK OF AFRICA (BMCE)", 2: "CIH"}


@pytest.mark.functionality
def test_a_journal_named_after_its_bank_gets_it_and_takes_the_statement(monkeypatch):
    written = []
    fake = fake_experio([{"id": 5, "name": "BQ BMCE", "bank_id": False, "partner_id": False}], written)
    monkeypatch.setattr(intake, "odoo_call", fake)
    monkeypatch.setattr(banks, "odoo_call", fake)
    monkeypatch.setattr(intake, "dry_run", lambda: False)
    monkeypatch.setattr(banks, "dry_run", lambda: False)
    journal, done = intake.statement_journal("r.pdf", "ATLAS", 857, 0)
    assert journal["id"] == 5 and written == [("exp.journal", "write", [5], {"bank_id": 1})]
    assert "journal `BQ BMCE` mis à la banque `BANK OF AFRICA (BMCE)` (d'après son nom)" in done


@pytest.mark.functionality
def test_a_statement_of_a_bank_the_dossier_has_no_journal_at_makes_its_journal(monkeypatch):
    written = []
    bmce = [{"id": 5, "name": "BMCE", "bank_id": [1, "BANK OF AFRICA (BMCE)"]}]
    monkeypatch.setattr(intake, "odoo_call", fake_experio(bmce, written))
    monkeypatch.setattr(banks, "odoo_call", fake_experio(bmce, written))
    monkeypatch.setattr(intake, "dry_run", lambda: False)
    monkeypatch.setattr(banks, "dry_run", lambda: False)
    journal, done = intake.statement_journal("releve_CIH_mars.pdf", "ATLAS", 857, 0)
    account = ("exp.account", "create", {"code": "51411000", "name": "Banque CIH", "company_id": 857})
    made = {"name": "Banque CIH", "code": "BQ1", "type": "bank", "company_id": 857, "bank_id": 2}
    made["default_account_id"] = 91
    assert written == [account, ("exp.journal", "create", made)]
    assert journal["id"] == 92 and journal["bank_id"] == [2, "CIH"]
    assert "Fait : journal de banque `Banque CIH` créé (code `BQ1`, compte `51411000`)" in done


@pytest.mark.functionality
def test_a_journal_refused_takes_back_the_account_made_for_it(monkeypatch):
    written = []
    fake = fake_experio([], written)

    def refusing(model, method, *args, **kw):
        if model == "exp.journal" and method == "create":
            raise RuntimeError("code déjà utilisé")
        return fake(model, method, *args, **kw)

    monkeypatch.setattr(banks, "odoo_call", refusing)
    monkeypatch.setattr(banks, "dry_run", lambda: False)
    with pytest.raises(RuntimeError):
        banks.make_bank_journal(857, BANKS[1], {})
    assert [w[1] for w in written] == ["create", "unlink"] and written[1][2] == [91]


@pytest.mark.security
def test_the_journal_given_must_be_a_usable_bank_journal_of_the_dossier(monkeypatch):
    usable = [{"id": 5, "name": "BMCE", "bank_id": [1, "BMCE"]}, {"id": 6, "name": "CDM", "bank_id": [2, "CDM"]}]
    monkeypatch.setattr(intake, "odoo_call", lambda *a, **k: usable)
    assert intake.statement_journal("r.pdf", "ATLAS", 857, 6) == (usable[1], "")
    with pytest.raises(ToolError, match="journal_id 9"):
        intake.statement_journal("r.pdf", "ATLAS", 857, 9)


@pytest.mark.communication
def test_bank_journals_unreachable_say_so(monkeypatch):
    def down(*args, **kw):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(intake, "odoo_call", down)
    with pytest.raises(ToolError, match="journaux de banque n'ont pas pu"):
        intake.bank_journals(857)


def tpe_file(monkeypatch, network):
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (True, network))
    return saved("tpe.pdf", pdf(1), "statement")


@pytest.mark.communication
def test_a_tpe_statement_without_network_asks_it(experio, monkeypatch):
    answer = intake.import_bank_statement(tpe_file(monkeypatch, None), 857)
    assert answer["status"] == "question" and "quel réseau" in answer["message"] and experio.calls == []


@pytest.mark.functionality
def test_a_tpe_statement_goes_to_the_journal_of_the_last_one_with_its_network(experio, monkeypatch):
    experio.journals = [{"id": 5, "name": "BMCE", "bank_id": [1, "B"]}, {"id": 6, "name": "CDM", "bank_id": [2, "C"]}]
    imported = []
    monkeypatch.setattr(intake.tpe, "last_journal", lambda dossier_id: 6)
    monkeypatch.setattr(intake.tpe, "import_tpe", lambda *args: imported.append(args[2:]) or 460)
    file_id = tpe_file(monkeypatch, "cmi")
    answer = intake.import_bank_statement(file_id, 857)
    assert (
        answer["statement_id"] == 460 and "relevé TPE CMI importé" in answer["message"] and "CDM" in answer["message"]
    )
    assert imported == [(857, 6, "cmi")] and row(file_id, "statement_id", "tpe_type") == (460, "cmi")


@pytest.mark.functionality
def test_a_tpe_network_given_wins_and_a_last_journal_not_usable_is_not_taken(experio, monkeypatch):
    experio.journals = [{"id": 5, "name": "BMCE", "bank_id": [1, "B"]}, {"id": 6, "name": "CDM", "bank_id": [2, "C"]}]
    monkeypatch.setattr(intake.tpe, "last_journal", lambda dossier_id: 99)
    monkeypatch.setattr(intake.tpe, "import_tpe", lambda *args: pytest.fail("never imported without a journal"))
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (False, None))
    file_id = saved("tpe.pdf", pdf(1), "statement")
    answer = intake.import_bank_statement(file_id, 857, network="naps")
    assert answer["status"] == "question" and set(answer["choices"]) == {5, 6}


@pytest.mark.security
def test_test_mode_imports_no_tpe_statement(experio, monkeypatch):
    monkeypatch.setattr(intake, "dry_run", lambda: True)
    monkeypatch.setattr(intake.tpe, "last_journal", lambda dossier_id: None)
    monkeypatch.setattr(intake.tpe, "import_tpe", lambda *args: pytest.fail("test mode imported"))
    file_id = tpe_file(monkeypatch, "vps")
    answer = intake.import_bank_statement(file_id, 857)
    assert answer["message"].startswith("MODE TEST") and "VPS" in answer["message"]
    assert row(file_id, "statement_id") == (None,)


@pytest.mark.communication
def test_a_tpe_import_refused_says_so_and_links_nothing(experio, monkeypatch):
    def refused(*args):
        raise RuntimeError("exp.monetary.statement.import 3 made [], expected one statement")

    monkeypatch.setattr(intake.tpe, "last_journal", lambda dossier_id: 5)
    monkeypatch.setattr(intake.tpe, "import_tpe", refused)
    file_id = tpe_file(monkeypatch, "cmi")
    with pytest.raises(ToolError, match="n'a pas pu importer le relevé TPE tpe.pdf"):
        intake.import_bank_statement(file_id, 857)
    assert row(file_id, "statement_id") == (None,)


@pytest.mark.functionality
def test_a_statement_of_a_bank_experio_cannot_read_is_not_imported(experio, monkeypatch):
    experio.bank_code = "bti"
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (False, None))
    answer = intake.import_bank_statement(saved("r.pdf", pdf(1), "statement"), 857)
    assert answer["status"] == "stop" and "ne lit pas encore les relevés de `BMCE" in answer["message"]
    assert not any(method == "create" for _, method, _, _ in experio.calls)


@pytest.mark.functionality
def test_experio_excel_template_is_imported_by_its_own_import(experio, monkeypatch):
    imported = []
    monkeypatch.setattr(
        intake, "import_canvas", lambda name, data, dossier_id, journal_id: imported.append(journal_id) or 88
    )
    file_id = saved("releve.xlsx", canvas(), "statement", mime="application/vnd.ms-excel")
    answer = intake.import_bank_statement(file_id, 857)
    assert answer["statement_id"] == 88 and "relevé Excel importé" in answer["message"] and imported == [5]
    assert row(file_id, "statement_id") == (88,)


@pytest.mark.security
def test_test_mode_imports_no_excel_template(experio, monkeypatch):
    monkeypatch.setattr(intake, "dry_run", lambda: True)
    monkeypatch.setattr(intake, "import_canvas", lambda *args: pytest.fail("test mode imported"))
    answer = intake.import_bank_statement(saved("releve.xlsx", canvas(), "statement"), 857)
    assert answer["message"].startswith("MODE TEST") and "relevé Excel serait importé" in answer["message"]


@pytest.mark.communication
def test_an_excel_template_refused_says_experio_reason(experio, monkeypatch):
    def refused(*args):
        raise RuntimeError("aucune ligne dans le canevas")

    monkeypatch.setattr(intake, "import_canvas", refused)
    file_id = saved("releve.xlsx", canvas(), "statement")
    with pytest.raises(ToolError, match=r"relevé releve.xlsx \(aucune ligne dans le canevas\)"):
        intake.import_bank_statement(file_id, 857)
    assert row(file_id, "statement_id") == (None,)


@pytest.mark.functionality
def test_a_pdf_statement_is_imported_by_experio_own_import_into_its_journal(experio, monkeypatch):
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (False, None))
    file_id = saved("releve.pdf", pdf(1), "statement")
    answer = intake.import_bank_statement(file_id, 857)
    assert answer["statement_id"] == 501 and row(file_id, "statement_id") == (501,)
    model, method, (values,), _ = next(call for call in experio.calls if call[1] == "create")
    assert (model, values["invoice_journal_id"], values["name"]) == ("exp.bank.statement.import", 5, "releve")


@pytest.mark.functionality
def test_one_statement_in_several_files_is_imported_once_as_the_client_says(experio, monkeypatch):
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (False, None))
    first = saved("p1.pdf", pdf(1), "statement", session="merge")
    second = saved("p2.pdf", pdf(2), "statement", session="merge")
    answer = intake.import_bank_statement(first, 857, client_message="c'est le même relevé en deux fichiers")
    assert "`p1.pdf` et `p2.pdf` : un seul relevé importé (n° 501)" in answer["message"]
    (values,) = next(call[2] for call in experio.calls if call[1] == "create")
    assert len(pypdfium2.PdfDocument(base64.b64decode(values["data"]))) == 3
    assert row(first, "statement_id") == (501,) and row(second, "statement_id") == (501,)


@pytest.mark.security
def test_files_are_merged_only_on_the_client_word(experio, monkeypatch):
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (False, None))
    first = saved("p1.pdf", pdf(1), "statement", session="nomerge")
    second = saved("p2.pdf", pdf(2), "statement", session="nomerge")
    intake.import_bank_statement(first, 857, client_message="voici les relevés")
    assert row(second, "statement_id") == (None,)


@pytest.mark.communication
def test_a_statement_import_refused_says_so_and_links_nothing(experio, monkeypatch):
    experio.refusal = ConnectionResetError("cut")
    monkeypatch.setattr(intake.tpe, "tpe_network", lambda raw, words: (False, None))
    file_id = saved("releve.pdf", pdf(1), "statement")
    with pytest.raises(ToolError, match="n'a pas pu importer le relevé releve.pdf"):
        intake.import_bank_statement(file_id, 857)
    assert row(file_id, "statement_id") == (None,)


@pytest.mark.security
@pytest.mark.parametrize("said", ["ci-joint le relevé de ce mois", "relevés BMCE et CIH"])
def test_a_journal_is_made_only_for_a_bank_named_in_whole_words(monkeypatch, said):
    written = []
    bmce = [{"id": 5, "name": "BMCE", "bank_id": [1, "BANK OF AFRICA (BMCE)"], "partner_id": False}]
    monkeypatch.setattr(intake, "odoo_call", fake_experio(bmce, written))
    monkeypatch.setattr(intake, "dry_run", lambda: False)
    journal, _ = intake.statement_journal("r.pdf", "ATLAS", 857, 0, said)
    assert written == [] and (journal.get("id") == 5 or journal["status"] == "question")  # never a CIH journal


@pytest.mark.functionality
def test_the_bank_named_goes_to_the_dossiers_one_journal_without_bank_not_a_new_one(monkeypatch):
    written = []
    fake = fake_experio([{"id": 5, "name": "BANQUE", "bank_id": False, "partner_id": False}], written)
    for module in (intake, banks):
        monkeypatch.setattr(module, "odoo_call", fake)
        monkeypatch.setattr(module, "dry_run", lambda: False)
    journal, done = intake.statement_journal("r.pdf", "ATLAS", 857, 0, "CIH")
    assert journal["id"] == 5 and written == [("exp.journal", "write", [5], {"bank_id": 2})]
    assert "journal `BANQUE` mis à la banque `CIH`" in done
