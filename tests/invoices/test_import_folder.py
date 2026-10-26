"""An import folder made as Experio's « Imports » screen makes it, and the rules its pieces follow after."""

import io

import pypdfium2
import pytest

from engine.tools.invoices import configuration, import_folder, posting, tiers


def pdf(pages):
    document = pypdfium2.PdfDocument.new()
    for _ in range(pages):
        document.new_page(595, 842)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("words", "mode"),
    [
        ("Voici le dossier d'import de mars", (True, False)),
        ("Ce sont plusieurs dossiers d'import, un dossier par fichier", (True, True)),
        ("Voici les factures d'achat importées", (False, False)),  # « importées »: never an import folder
        ("", (False, False)),
    ],
)
def test_an_import_folder_is_the_clients_word_never_a_guess(words, mode):
    assert import_folder.folder_mode(words) == mode


@pytest.mark.functionality
def test_pages_are_grouped_into_documents_a_new_one_at_each_start_or_type(monkeypatch):
    answers = iter([("transit", True), ("transit", False), ("customs", False), ("dum", True), ("dum", True)])
    monkeypatch.setattr(import_folder, "pdf_pages", lambda raw, limit: ["p"] * 5)
    monkeypatch.setattr(import_folder, "typed_page", lambda image, kinds, dossier, what: next(answers))
    documents = import_folder.documents_of(b"", {}, "D", "f.pdf")
    # transit over 2 pages; customs starts its own though the model said it continues (another type); 2 DUMs
    assert documents == [("transit", [0, 1]), ("customs", [2]), ("dum", [3]), ("dum", [4])]


@pytest.mark.functionality
def test_experios_own_folder_import_sequence(monkeypatch):
    calls, ids = [], iter([101, 102])

    def odoo_json(model, method, *args, **kw):
        calls.append((model, method, kw["context"].get("defer_bluejay_send", False)))
        if method == "action_add_documents_from_javascript":
            assert args[1][0]["import_invoice_type"] in ("transit", "supplier")
            return [next(ids)]
        if method.endswith("_from_javascript") and model == "exp.folder.import":
            assert args[0] == [77]  # the wizard's id, as Experio's screen gives it
            assert [a["name"] for a in args[1]] == ["dum.pdf", "bl.pdf"]  # its DUM reading needs its screen
        if method == "create_folder_from_js":
            assert args[0]["invoice_ids"] == [[6, 0, [101, 102]]] and args[0]["folder_number"] is False
            return 9
        return True

    monkeypatch.setattr(import_folder, "odoo_json", odoo_json)
    monkeypatch.setattr(import_folder, "odoo_call", lambda *a, **k: 77)
    documents = [
        (1, "t.pdf", "transit", pdf(1)),
        (1, "dum.pdf", "dum", pdf(1)),
        (1, "s.pdf", "supplier", pdf(1)),
        (1, "bl.pdf", "attachment", pdf(1)),
    ]
    folder_id, made = import_folder.create_in_experio(5, [(1, "f.pdf", pdf(4))], documents, "transit_invoice")
    assert folder_id == 9 and [m[2] for m in made] == [101, 102]
    assert calls == [
        ("exp.invoice.import", "action_add_documents_from_javascript", True),  # reading held until the folder
        ("exp.invoice.import", "action_add_documents_from_javascript", True),
        ("exp.import.folder", "create_folder_from_js", False),  # which sends their reading
        ("exp.folder.import", "action_attach_pages_from_javascript", False),  # the DUM with the attachments
    ]


@pytest.mark.functionality
def test_the_transit_agents_invoice_comes_last():
    made = [(1, "t", 11), (1, "c", 12), (1, "s", 13)]
    assert import_folder.transit_last(made, {11: "transit", 12: "customs"}) == [
        (1, "c", 12),
        (1, "s", 13),
        (1, "t", 11),
    ]


@pytest.mark.functionality
def test_a_folder_without_number_takes_its_transit_invoices_as_experios_reading_does(monkeypatch):
    written = []

    def odoo_call(model, method, *args, **kw):
        if model == "exp.invoice" and method == "read":
            return [{"folder_id": [4, "F"]}]
        if model == "exp.import.folder" and method == "read":
            return [{"id": 4, "folder_number": False, "folder_type": "import", "company_id": [5, "D"]}]
        if model == "res.company":
            return [{"import_folder_number": "transit_invoice"}]
        if method == "search_read":
            assert ("invoice_type", "=", "transit") in args[0]
            return [{"invoice_number": "TR-2026-88"}]
        written.append(args)

    monkeypatch.setattr(configuration, "odoo_call", odoo_call)
    monkeypatch.setattr(configuration, "dry_run", lambda: False)
    assert configuration.settle_folder_number(3, {}) == "numéro du dossier d'import : TR-2026-88"
    assert written == [([4], {"folder_number": "TR-2026-88"})]


@pytest.mark.security
def test_the_transit_invoice_waits_for_the_invoices_it_paid_then_takes_them(monkeypatch):
    state = {"drafts": [21], "posted": [22, 23], "written": [], "refreshed": 0}
    transit = {"invoice_type": "transit", "folder_id": [4, "F"], "folder_type": "import", "company_id": [5, "D"]}
    transit.update(exercice_id=False, disbursement_invoice_ids=[23])

    def odoo_call(model, method, *args, **kw):
        if method == "read":
            return [transit]
        if method == "write":
            state["written"].append(args[1])
            return True
        states = dict(c[1:] for c in [("x", c[0], c[2]) for c in args[0]])
        return state["drafts"] if states.get("state") == ["pending", "draft"] else state["posted"]

    monkeypatch.setattr(posting, "odoo_call", odoo_call)
    monkeypatch.setattr(posting, "dry_run", lambda: False)
    monkeypatch.setattr(posting, "invoice_context", lambda invoice: {})
    monkeypatch.setattr(posting, "refresh", lambda i, c: state.__setitem__("refreshed", state["refreshed"] + 1))
    stopped = posting.disbursements_first({"id": 20})
    assert stopped["status"] == "stop" and "21" in stopped["message"] and state["written"] == []
    state["drafts"] = []
    assert posting.disbursements_first({"id": 20}) is None
    assert state["written"] == [{"disbursement_invoice_ids": [(4, 22)]}] and state["refreshed"] == 1  # 23: linked


@pytest.mark.functionality
def test_a_customs_invoice_takes_the_dossiers_customs_tiers(monkeypatch):
    found = [{"id": 8, "name": "ADMINISTRATION DES DOUANES ET IMPOTS INDIRECTS", "ice": False}]
    monkeypatch.setattr(tiers, "dossier_tiers", lambda company_id: found + [{"id": 9, "name": "MAERSK", "ice": False}])
    monkeypatch.setattr(
        tiers, "link", lambda invoice, partner_id, kind: {"status": "ok", "message": f"fournisseur {partner_id}"}
    )
    invoice = {"id": 3, "company_id": [5, "D"]}
    assert tiers.customs_tiers(invoice)["message"] == "administration des douanes 8"
    found.append({"id": 10, "name": "Douane - port", "ice": False})  # two: asked, never chosen
    assert tiers.customs_tiers(invoice)["status"] == "question"
    found.clear()
    assert tiers.customs_tiers(invoice) is None  # none: the usual steps


@pytest.mark.functionality
def test_the_same_folder_file_sent_again_makes_no_second_folder(db_cabinet, monkeypatch):
    from engine.tools.core.cabinets import cabinet_db

    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind, folder_id) "
            "values ('s', 'imp.pdf', 'application/pdf', %s, 'folder', 1425)",
            (b"%PDF same",),
        )
        (again,) = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data) values ('s2', 'imp (1).pdf', "
            "'application/pdf', %s) returning id",
            (b"%PDF same",),
        ).fetchone()
    monkeypatch.setattr(import_folder, "odoo_call", lambda model, method, domain, **kw: [1425])  # still in the dossier
    assert import_folder.folder_twins(857, [(again, "imp (1).pdf", b"%PDF same")]) == [("imp (1).pdf", 1425)]
    with cabinet_db() as db:
        assert db.execute("select kind, folder_id from tarik_files where id = %s", (again,)).fetchone() == (
            "folder",
            1425,
        )


@pytest.mark.functionality
def test_a_tiers_name_experio_cut_short_is_taken_whole_from_the_check(monkeypatch):
    monkeypatch.setattr(tiers, "stored_check", lambda i: (True, {"tier": "FOCQUET SA"}, [{"field": "tier"}]))
    assert tiers.flagged_tier(3) == "FOCQUET SA"
    monkeypatch.setattr(tiers, "stored_check", lambda i: (True, {"tier": "FOCQUET SA"}, []))  # not flagged: Experio's
    assert tiers.flagged_tier(3) is None
