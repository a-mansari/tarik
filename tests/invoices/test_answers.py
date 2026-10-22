"""The client's answers to what a conversation waits on, on the real Postgres (db_cabinet)."""

import pytest

from tarik.core.app import ok, stop
from tarik.core.cabinets import cabinet_db
from tarik.core.conversation import yes_no
from tarik.invoices import exercice, pipeline


@pytest.fixture
def answers(db_cabinet, monkeypatch):
    """Experio's invoices 11 (ATLAS) and 12 (NOVA); every answer applied is recorded, the sender is allowed."""
    applied = []
    partners = {11: "ATLAS TRANSPORT - 1234", 12: "NOVA TELECOM", 20: "ALPHA", 35: "BETA"}

    def odoo_call(model, method, query, **kw):
        if method == "search":  # the drafts among the ids asked: 99 was deleted in Experio
            return [i for i in query[0][2] if i in partners]
        return [{"id": i, "invoice_number": f"FA-{i}", "partner_id": [i, partners[i]]} for i in query]

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: ok("ok"))

    def record(invoice_id, answer, *asked):
        applied.append((invoice_id, answer))
        return ok("applique")

    monkeypatch.setitem(pipeline.ANSWERS, "assign_account", record)
    monkeypatch.setitem(pipeline.ANSWERS, "settle_tva", record)
    return applied


def waits(invoice_id, step, text, session="s"):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_pending (invoice_id, session_id, step, question) values (%s, %s, %s, %s)",
            (invoice_id, session, step, text),
        )


@pytest.mark.functionality
def test_the_only_question_gets_the_answer_and_the_sequence_resumes(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    result = pipeline.answer_pending("s", "le 6125\n\nFichiers joints : ICE 001122334455667.pdf")
    assert answers == [(11, "le 6125")] and result["next_step"] == "settle_tva" and result["invoice_id"] == 11


@pytest.mark.functionality
def test_with_several_questions_the_answer_names_its_invoice(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    waits(12, "settle_tva", "Facture de NOVA TELECOM : la TVA est-elle non récupérable ? oui / non")
    which = pipeline.answer_pending("s", "oui pour Nova Telecom", "oui")  # the agent didn't say which: asked
    assert which["status"] == "question" and "- facture 12 : Facture de NOVA TELECOM" in which["message"]
    assert answers == []
    assert pipeline.answer_pending("s", "oui pour Nova Telecom", "oui", "12")["invoice_id"] == 12
    assert answers == [(12, "oui")]


@pytest.mark.security
@pytest.mark.parametrize("decision", ["", "Ok non merci", "peut-être"])
def test_a_oui_non_question_the_agents_decision_does_not_settle_is_asked_again(answers, decision):
    waits(12, "settle_tva", "Facture de NOVA TELECOM : la TVA est-elle non récupérable ? oui / non")
    result = pipeline.answer_pending("s", "Ok non merci", decision)
    assert result["status"] == "question" and result["message"].startswith("Réponse non comprise. Facture de NOVA")
    assert answers == []  # nothing applied: the client is asked again


@pytest.mark.functionality
def test_a_plain_oui_is_read_even_when_the_agent_left_the_decision_empty(answers):
    """The agent called the wrong tool for an answer: its decision is empty, the client's "Oui" is still exact."""
    waits(12, "settle_tva", "Facture de NOVA TELECOM : la TVA est-elle non récupérable ? oui / non")
    result = pipeline.answer_pending("s", "Oui", "")
    assert answers == [(12, "Oui")] and result["next_step"] == "check_journal"


@pytest.mark.security
def test_only_a_plain_oui_or_non_decides():
    assert yes_no("oui") is True and yes_no(" Oui ") is True and yes_no("non") is False
    for unclear in ("Ok non merci", "Nous sommes d'accord", "non récupérable", "oui... enfin non", "", None):
        assert yes_no(unclear) is None, unclear  # the agent reads the reply and says "oui" or "non", or it is asked


@pytest.mark.security
def test_another_conversations_question_is_never_answered(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?", session="other")
    assert pipeline.answer_pending("s", "6125")["next_step"] == "waiting_drafts" and answers == []


@pytest.mark.security
def test_a_sender_without_the_dossier_changes_nothing_and_is_recorded_as_refused(answers, monkeypatch):
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: stop("pas ce dossier"))
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    refused = pipeline.answer_pending("s", "6125")
    assert (
        refused["message"] == "pas ce dossier" and refused["step"] == "check_sender" and answers == []
    )  # the manager is told


@pytest.mark.security
def test_only_the_invoice_the_agent_names_among_those_waiting_is_answered(answers):
    waits(20, "settle_tva", "Facture FA-20 : la TVA est-elle non récupérable ? oui / non")
    waits(35, "settle_tva", "Facture FA-35 : la TVA est-elle non récupérable ? oui / non")
    for about in ("", "99", "FA-20 ou FA-35"):  # none, one not waiting, not one
        assert pipeline.answer_pending("s", "oui, TVA 20 %", "oui", about)["status"] == "question" and answers == []
    assert pipeline.answer_pending("s", "oui pour la facture 35", "oui", "facture 35")["invoice_id"] == 35


@pytest.mark.functionality
def test_a_question_on_an_invoice_deleted_since_is_dropped(answers):
    waits(99, "assign_account", "Facture 99 : quel compte utiliser ?")
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    assert pipeline.answer_pending("s", "6125")["invoice_id"] == 11  # the only one left
    with cabinet_db() as db:
        assert db.execute("select invoice_id from tarik_pending").fetchall() == [(11,)]


@pytest.mark.functionality
def test_a_statement_gets_the_bank_account_the_client_names(answers, monkeypatch):
    with cabinet_db() as db:
        file_id = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind) values "
            "('s', 'relevé mars.pdf', 'application/pdf', '', 'statement') returning id"
        ).fetchone()[0]
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, dossier_id, step, status, message, choices) "
            "values ('r', 's', %s, 557, 'import_bank_statement', 'question', 'relevé mars.pdf : lequel ? BMCE - BMCE "
            "BANK (n° 4), BP - BANQUE POPULAIRE (n° 5)', %s)",
            (file_id, '{"4": "BMCE - BMCE BANK", "5": "BP - BANQUE POPULAIRE"}'),
        )
    monkeypatch.setattr(
        pipeline,
        "import_bank_statement",
        lambda file_id, dossier_id, journal_id: ok(f"{file_id} {dossier_id} {journal_id}"),
    )
    assert pipeline.answer_pending("s", "j'ai 7 releves, compte BMCE", "7")["message"] == f"{file_id} 557 0"  # unlisted
    assert pipeline.answer_pending("s", "Le compte BMCE", "4")["message"] == f"{file_id} 557 4"
    pipeline.record_step(
        "r2",
        "s",
        "Reprendre ou ?",
        {"status": "ok", "message": "MODE TEST : importe", "file_id": file_id, "step": "import_bank_statement"},
    )  # test mode: no statement saved
    assert pipeline.waiting_on("s") == []  # answered: it waits no more


@pytest.mark.functionality
def test_a_file_the_client_says_is_no_statement_becomes_an_invoice_without_any_import(answers, monkeypatch):
    with cabinet_db() as db:
        file_id = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind) values "
            "('s', 'douane.pdf', 'application/pdf', '', 'statement') returning id"
        ).fetchone()[0]
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, dossier_id, step, status, message, choices) "
            "values ('r', 's', %s, 557, 'import_bank_statement', 'question', 'douane.pdf : lequel ?', %s)",
            (file_id, '{"4": "BMCE - BMCE BANK"}'),
        )
    monkeypatch.setattr(pipeline, "import_bank_statement", lambda *args: pytest.fail("never imported"))
    said = "Ce n'est pas un relevé bancaire, c'est une facture d'achat."
    result = pipeline.answer_pending("s", said, "")
    assert result["status"] == "ok" and result["next_step"] == "identify_document"
    assert (result["file_id"], result["dossier_id"], result["invoice_type"]) == (file_id, 557, "achat")
    with cabinet_db() as db:
        assert db.execute("select kind from tarik_files where id = %s", (file_id,)).fetchone() == ("invoice",)
    assert pipeline.waiting_on("s") == []  # no bank account asked any more


@pytest.mark.functionality
def test_the_date_the_client_gives_is_the_invoices(monkeypatch):
    written = []
    monkeypatch.setattr(
        exercice,
        "require_draft_invoice",
        lambda invoice_id, fields, what: {
            "state": "draft",
            "company_id": [1, "X"],
            "exercice_id": False,
            "invoice_date": False,
            "is_old_invoice": False,
        },
    )
    monkeypatch.setattr(
        exercice,
        "odoo_call",
        lambda *a, **k: [{"id": 5, "name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31"}],
    )
    monkeypatch.setattr(exercice, "fill_invoice", lambda invoice_id, values, context: written.append(values))
    monkeypatch.setattr(exercice, "test_mode_note", lambda: "")
    assert exercice.settle_exercice(7, "")["status"] == "question"
    assert exercice.settle_exercice(7, "du 15/03/2026")["message"].endswith("rattachée à l'exercice Exercice 2026.")
    assert written == [{"invoice_date": "2026-03-15"}, {"exercice_id": 5}]


@pytest.mark.functionality
def test_a_draft_that_failed_waited_or_was_cut_short_is_taken_up_again(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain, **kw: domain[0][2])  # all still drafts
    for run, invoice_id, step, status in [
        ("r", 1, "check_invoice", "error"),
        ("r", 1, "wait_until_read", "ok"),
        ("r", 2, "wait_until_read", "wait"),
        ("r", 3, "settle_tva", "question"),
        ("r", 4, "check_invoice", "error"),
        ("r2", 4, "post_invoice", "ok"),
    ]:
        pipeline.record_step(run, "s", step, {"status": status, "message": "m", "invoice_id": invoice_id})
    with cabinet_db() as db:  # 5: its run was stopped before recording anything; 4's file is not resumed twice
        for invoice_id in (4, 5):
            db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) "
                "values ('s', 'f.pdf', 'application/pdf', '', %s)",
                (invoice_id,),
            )
    assert pipeline.waiting_drafts("s")["invoices"] == [1, 2, 5]  # 3 waits on the client, 4 went through since


@pytest.mark.functionality
def test_a_run_waits_on_experio_reading_once_for_all_its_drafts(monkeypatch):
    clock, states = [0.0], {1: "pending", 2: "pending", 3: "done"}
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: {"bluejay_state": states[invoice_id]})
    monkeypatch.setattr(pipeline, "READ_DEADLINES", {})
    monkeypatch.setattr(pipeline, "split_off", lambda invoice_id: [])
    assert pipeline.wait_until_read(1, "run")["status"] == "wait"
    waited = clock[0]
    assert pipeline.READ_WAIT <= waited < pipeline.READ_WAIT + 15  # one stuck reading never holds the run
    assert pipeline.wait_until_read(2, "run")["status"] == "wait" and clock[0] == waited  # the deadline is passed
    assert pipeline.wait_until_read(3, "run")["status"] == "ok"
    assert pipeline.wait_until_read(2, "next run")["status"] == "wait" and clock[0] > waited  # its own deadline


@pytest.mark.functionality
def test_a_bank_journals_answer_sets_its_bank_then_asks_the_next(db_cabinet, monkeypatch):
    pipeline.record_step(
        "r",
        "s",
        "ask_banks",
        {
            "status": "question",
            "message": "Dossier Gecafisc : le journal BQ1 est a quelle banque ? CIH (n° 3), BP (n° 5).",
            "dossier_id": 557,
            "journal_id": 11,
            "choices": {3: "CIH", 5: "BP"},
        },
    )
    monkeypatch.setattr(pipeline, "journal_name", lambda journal_id: "BQ1")
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id, invoice_id: {"status": "ok"})
    monkeypatch.setattr(pipeline, "odoo_call", lambda *a, **k: [])
    set_calls = []
    monkeypatch.setattr(
        pipeline, "set_journal_bank", lambda d, j, b: set_calls.append((d, j, b)) or ok("Dossier Gecafisc : BQ1 a CIH.")
    )
    monkeypatch.setattr(pipeline, "ask_banks", lambda d: {"status": "question", "message": "again"})
    waiting = pipeline.waiting_on("s")
    assert [item["label"] for item in waiting] == ["journal BQ1"] and waiting[0]["choices"] == {"3": "CIH", "5": "BP"}
    assert pipeline.answer_pending("s", "c'est le CIH", "3")["message"] == "Dossier Gecafisc : BQ1 a CIH."
    assert set_calls == [(557, 11, 3)]
    assert pipeline.answer_pending("s", "je ne sais plus", "")["message"] == "again"  # asked again
    monkeypatch.setattr(pipeline, "journal_name", lambda journal_id: None)  # given its bank since
    assert pipeline.waiting_on("s") == []


@pytest.mark.functionality
def test_an_invoice_stays_a_verified_draft_until_the_client_says_to_post_it(db_cabinet, monkeypatch):
    searched = []

    def search(model, method, domain, **kw):
        if method == "search_read":  # the dossier's drafts, when nothing is to be posted: none left
            return []
        searched.append(domain)
        return domain[0][2]  # all still drafts

    monkeypatch.setattr(pipeline, "odoo_call", search)
    assert pipeline.hold_posting(7, "s")["status"] == "stop"  # nothing said: imported and verified only
    assert pipeline.hold_posting(7, "s")["status"] == "stop"  # a later run carries no answer: still held
    pipeline.record_step("r", "s", "hold_posting", {"status": "stop", "message": "m", "invoice_id": 7})
    assert pipeline.waiting_drafts("s")["invoices"] == []  # a stop is not resumed...
    assert pipeline.waiting_drafts("s", held=True)["invoices"] == [7]  # ...unless they now ask to post
    assert pipeline.waiting_drafts("other", held=True)["invoices"] == []  # another conversation needs the dossier
    assert pipeline.waiting_drafts("other", held=True, dossier_id=5)["invoices"] == []  # ...and to ask for it
    # ...in any conversation, when they ask for the whole dossier: only its drafts
    assert pipeline.waiting_drafts(
        "other", held=True, dossier_id=5, client_message="Comptabilisez toutes les factures du dossier."
    )["invoices"] == [7]
    assert ("company_id", "in", [5]) in searched[-1]
    assert pipeline.hold_posting(7, "s", "oui")["status"] == "ok"
    assert pipeline.hold_posting(7, "s")["status"] == "ok"  # kept for the runs resumed after a question
    nothing = pipeline.waiting_drafts(
        "other", held=True, dossier_id=5, client_message="Comptabilisez toutes les factures du dossier."
    )
    assert nothing["invoices"] == [] and nothing["status"] == "stop"  # nothing left to post, and they are told
    assert pipeline.hold_posting(7, "s", "non")["status"] == "stop"  # taken back


@pytest.mark.functionality
def test_stopped_drafts_are_resumed_only_when_the_client_asks(db_cabinet, monkeypatch):
    searched = []

    def search(model, method, domain, **kw):
        searched.append(domain)
        return domain[0][2]  # all still drafts

    monkeypatch.setattr(pipeline, "odoo_call", search)
    for session, invoice_id, step, status in [
        ("s", 1, "assign_account", "stop"),  # its cabinet fixed the journal since
        ("s", 2, "hold_posting", "stop"),  # kept unposted: held, not stopped
        ("s", 3, "check_duplicate", "stop"),  # a real duplicate: the client's call, not a setting
        ("old", 4, "assign_account", "stop"),  # another conversation, same dossier
        ("old", 5, "assign_account", "stop"),  # stopped once, went through since
        ("old", 5, "post_invoice", "ok"),
    ]:
        pipeline.record_step("r", session, step, {"status": status, "message": "m", "invoice_id": invoice_id})
    assert pipeline.waiting_drafts("s")["invoices"] == []  # a follow-up never retries a stop
    assert pipeline.waiting_drafts("s", stopped=True)["invoices"] == [1]  # this conversation's, asked for
    assert pipeline.waiting_drafts("new", stopped=True, dossier_id=5)["invoices"] == []  # others': only if asked
    reprenez = "Reprenez toutes les factures du dossier."
    assert pipeline.waiting_drafts("new", stopped=True, dossier_id=5, client_message=reprenez)["invoices"] == [1, 4]
    assert ("company_id", "in", [5]) in searched[-1]
    assert pipeline.waiting_drafts("new", stopped=True)["invoices"] == []  # no dossier: nothing from elsewhere


@pytest.mark.functionality
def test_a_draft_tarik_posted_and_experio_shows_as_draft_again_is_posted_again(db_cabinet, monkeypatch):
    """Experio decides what is a draft: an invoice Tarik posted, put back to draft in Experio since, is posted when
    the client asks, from any conversation, for its dossier only."""
    # All still drafts; the dossier's drafts listed when nothing is to be posted: none.
    monkeypatch.setattr(
        pipeline, "odoo_call", lambda model, method, domain, **kw: [] if method == "search_read" else domain[0][2]
    )
    for step in ("hold_posting", "post_invoice"):
        pipeline.record_step("r", "earlier", step, {"status": "ok", "message": "m", "invoice_id": 9})
    assert pipeline.waiting_drafts("new", held=True, dossier_id=5)["invoices"] == []  # not asked for
    assert pipeline.waiting_drafts(
        "new", held=True, dossier_id=5, client_message="Comptabilisez toutes les factures du dossier."
    )["invoices"] == [9]
    assert pipeline.waiting_drafts("new", held=True)["invoices"] == []  # without a dossier: never another's


@pytest.mark.communication
def test_when_nothing_is_to_be_posted_the_client_is_told_why(db_cabinet, monkeypatch):
    drafts = [{"id": 1, "invoice_number": "F-1"}, {"id": 2, "invoice_number": "F-2"}, {"id": 3, "invoice_number": None}]

    def odoo_call(model, method, domain, **kw):
        return drafts if method == "search_read" else []

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) values "
            "('s', 'a.pdf', 'application/pdf', '', 1), ('s', 'b.pdf', 'application/pdf', '', 2)"
        )
    waits(1, "assign_account", "quel compte ?")
    pipeline.record_step(
        "r", "s", "check_duplicate", {"status": "stop", "message": "Facture 2 : doublon.", "invoice_id": 2}
    )
    told = pipeline.waiting_drafts("s", held=True, dossier_id=5)
    assert told["status"] == "stop" and told["invoices"] == []
    assert "facture 1 (F-1) : attend votre réponse à sa question : quel compte ?" in told["message"]
    assert "facture 2 (F-2) : Facture 2 : doublon." in told["message"]
    assert "facture 3 (sans numéro) : pas importée par Tarik" in told["message"]


@pytest.mark.communication
def test_a_draft_another_run_is_working_on_is_said_in_progress(db_cabinet, monkeypatch):
    drafts = [{"id": 2, "invoice_number": "F-2"}]
    monkeypatch.setattr(
        pipeline, "odoo_call", lambda model, method, domain, **kw: drafts if method == "search_read" else []
    )
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) values "
            "('s', 'b.pdf', 'application/pdf', '', 2)"  # this conversation's draft
        )
    pipeline.record_step(  # run A (another conversation's, posting the whole dossier) is working on it
        "A", "other", "create_draft_invoice", {"status": "ok", "message": "brouillon 2 créé", "invoice_id": 2}
    )
    told = pipeline.waiting_drafts("s", held=True, dossier_id=5, run_id="B")["message"]
    assert "facture 2 (F-2) : en cours de traitement" in told and "brouillon 2 créé" not in told


@pytest.mark.functionality
def test_posting_takes_the_conversations_own_held_drafts_first(db_cabinet, monkeypatch):
    """ "Comptabilisez-la" in a thread is about that thread's invoice: the dossier's drafts held in other
    conversations are taken only when the client asks for the whole dossier, or names them."""
    # All still drafts; the dossier's drafts listed when nothing is to be posted: none.
    monkeypatch.setattr(
        pipeline, "odoo_call", lambda model, method, domain, **kw: [] if method == "search_read" else domain[0][2]
    )
    pipeline.hold_posting(7, "this")
    pipeline.hold_posting(8, "another")
    assert pipeline.waiting_drafts("this", held=True, dossier_id=5)["invoices"] == [7]
    assert pipeline.waiting_drafts("new", held=True, dossier_id=5)["invoices"] == []
    told = pipeline.waiting_drafts("new", held=True, dossier_id=5, client_message="Comptabilisez-les.")["message"]
    assert "2 brouillon(s) d'autres conversations attendent" in told
    assert "« comptabilisez toutes les factures du dossier »" in told
    assert pipeline.waiting_drafts(
        "new", held=True, dossier_id=5, client_message="Comptabilisez toutes les factures du dossier."
    )["invoices"] == [7, 8]


@pytest.mark.security
def test_drafts_of_other_conversations_are_taken_only_when_the_client_names_them(db_cabinet, monkeypatch):
    drafts = {7: ("F-77", [1, "MIDAS - 4411M005"]), 8: ("AB-2026-12", [2, "EXELENTIA INTERNATIONAL TRANS"])}

    def odoo_call(model, method, domain, **kw):
        if method == "search_read" and domain[0][0] == "id":
            return [{"id": i, "invoice_number": drafts[i][0], "partner_id": drafts[i][1]} for i in domain[0][2]]
        return [] if method == "search_read" else domain[0][2]  # nothing to post: the dossier's drafts, none

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    pipeline.hold_posting(7, "another")
    pipeline.hold_posting(8, "another")
    for said, taken in [
        ("Comptabilisez la facture MIDAS.", [7]),
        ("Comptabilisez celle d'Exelentia.", [8]),  # by its first distinctive word
        ("Comptabilisez celles du Maroc.", []),  # a word too common to name a supplier
        ("Comptabilisez la AB-2026-12 et la F-77.", [7, 8]),
        ("Comptabilisez toutes les factures de Midas.", [7]),  # filtered: a supplier's, not the whole dossier
        ("Comptabilisez-la.", []),  # this thread's: none held here
        ("Comptabilisez les factures du dossier TEST.", []),  # naming the dossier is not asking for all of it
        ("Comptabilisez toutes les factures du dossier TEST.", [7, 8]),
        ("Comptabilisez toutes les factures du fournisseur Exelentia.", [8]),  # filtered, not the whole dossier
    ]:
        assert pipeline.waiting_drafts("new", held=True, dossier_id=5, client_message=said)["invoices"] == taken, said


@pytest.mark.security
def test_a_correction_naming_another_file_never_flips_the_statement_asked_about(answers, monkeypatch):
    with cabinet_db() as db:
        insert = (
            "insert into tarik_files (session_id, file_name, mime_type, data, kind) values "
            "('s', %s, 'application/pdf', '', %s) returning id"
        )
        statement = db.execute(insert, ("releve.pdf", "statement")).fetchone()[0]
        db.execute(insert, ("douane.pdf", "invoice"))
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, dossier_id, step, status, message, choices) "
            "values ('r', 's', %s, 557, 'import_bank_statement', 'question', 'releve.pdf : lequel ?', %s)",
            (statement, '{"4": "BMCE - BMCE BANK"}'),
        )
    monkeypatch.setattr(pipeline, "import_bank_statement", lambda *args: ok("asked again"))
    assert (
        pipeline.answer_pending("s", "douane.pdf n'est pas un relevé, c'est une facture.", "")["message"]
        == "asked again"
    )
    with cabinet_db() as db:
        assert db.execute("select kind from tarik_files where id = %s", (statement,)).fetchone() == ("statement",)
    assert (
        pipeline.answer_pending("s", "Ce n'est pas un relevé, c'est une facture.", "")["next_step"]
        == "identify_document"
    )


@pytest.fixture
def three_drafts(db_cabinet, monkeypatch):
    """A conversation's drafts 272459 (STATION SERVICE), 272469 (SRM-CS) and 272471 (ZHEJIANG), as Experio reads
    them."""
    read = {
        272459: ("19138", "STATION SERVICE - 4411S002"),
        272469: ("1579653218", "SRM-CS"),
        272471: ("CD241122", "ZHEJIANG WUMA DRIVE"),
    }
    with cabinet_db() as db:
        for invoice_id in read:
            db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) "
                "values ('s', 'f.pdf', 'application/pdf', '', %s)",
                (invoice_id,),
            )
    rows = [{"id": i, "invoice_number": number, "partner_id": [1, name]} for i, (number, name) in read.items()]
    rows[2]["invoice_number"] = "CD.24.1122"  # a number with dots, kept whole in a clause

    def odoo_call(model, method, domain, **kw):  # search_read by ids: a deleted draft (272450) is simply absent
        return [row for row in rows if row["id"] in domain[0][2]]

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: ok("ok"))
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) "
            "values ('s', 'gone.pdf', 'application/pdf', '', 272450)"
        )
        # Another conversation's draft whose question waits in this one: it is this conversation's to name too.
        db.execute("insert into tarik_pending (invoice_id, session_id, step, question) values (272463, 's', 'x', 'q')")
    rows.append({"id": 272463, "invoice_number": "F-0125-2544243", "partner_id": [2, "ORANGE MAROC"]})


@pytest.mark.functionality
@pytest.mark.parametrize(
    "said, read, fixed",
    [  # what the client wrote, what the agent read in it ([{facture, compte}]), what is applied
        (
            "Pour la 272469 : non. Et mettez la 272471 en compte 61425000 Transport sur achats.",
            [{"facture": "272471", "compte": "61425000 Transport sur achats"}],
            {272471: "61425000 Transport sur achats"},
        ),
        (
            "Oui pour Station Service, et mettez la CD.24.1122 en 61425000.",
            [{"facture": "CD.24.1122", "compte": "61425000"}],
            {272471: "61425000"},
        ),
        ("Non, et mettez-la en 6125.", [{"facture": "", "compte": "6125"}], {272469: "6125"}),  # the one answered
        (
            "Non, et mettez celle-ci en frais de péage.",
            [{"facture": "celle-ci", "compte": "frais de péage"}],
            {272469: "frais de péage"},
        ),
        (
            "Oui, la Station Service en frais de péage.",
            [{"facture": "Station Service", "compte": "frais de péage"}],
            {272459: "frais de péage"},
        ),
        (
            "Pour la 272469 : non. La F-0125-2544243 en 61455000.",
            [{"facture": "F-0125-2544243", "compte": "61455000"}],
            {272463: "61455000"},
        ),  # asked here
        ("Non. La F-0999-1234567 en 61455000.", [{"facture": "F-0999-1234567", "compte": "61455000"}], {}),  # unknown
        ("Non, elle est récupérable.", [{"facture": "272471", "compte": "61425000"}], {}),  # not the client's words
        ("Non, mettez la 272466 en 6125.", [{"facture": "272466", "compte": "6125"}], {}),  # not this conversation's
        (
            "Non, mettez celle-ci en Entretien et réparations ; la 272471 en 61425000.",
            [{"facture": "", "compte": "Entretien et réparations"}, {"facture": "272471", "compte": "61425000"}],
            {272469: "Entretien et réparations", 272471: "61425000"},
        ),
    ],
)
def test_the_accounts_the_agent_reads_are_applied_only_as_the_client_wrote_them(three_drafts, said, read, fixed):
    assert pipeline.verified_corrections("s", said, read, 272469)[0] == fixed


@pytest.mark.security
def test_an_account_for_an_invoice_of_a_dossier_the_sender_may_not_work_on_is_never_applied(three_drafts, monkeypatch):
    refused = {"status": "stop", "message": "pas autorisé"}
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: refused)
    said = "Oui. La 272471 en 61425000."
    assert pipeline.verified_corrections("s", said, [{"facture": "272471", "compte": "61425000"}], 272469)[0] == {}


@pytest.mark.communication
def test_an_account_whose_invoice_is_not_found_is_said_left_unchanged(three_drafts):
    said = "Non. La F-0999-1234567 en 61455000."
    _, notes = pipeline.verified_corrections("s", said, [{"facture": "F-0999-1234567", "compte": "61455000"}], 272469)
    assert notes == ["Le compte « 61455000 » : précisez pour quelle facture (son numéro), je n'ai rien changé."]


@pytest.mark.functionality
def test_an_account_given_while_nothing_waits_still_goes_to_the_invoice_named(three_drafts, monkeypatch):
    monkeypatch.setattr(pipeline, "waiting_on", lambda session_id: [])
    read = [{"facture": "272471", "compte": "61425000"}]
    result = pipeline.answer_pending("s", "Mettez la 272471 en compte 61425000.", corrections=read)
    assert result["next_step"] == "waiting_drafts" and result["corrections"] == [[272471, "61425000"]]
    none = pipeline.answer_pending("s", "Mettez-la en 61425000.", corrections=[{"facture": "", "compte": "61425000"}])
    assert "corrections" not in none and "précisez pour quelle facture" in none["message"]  # none answered about


@pytest.mark.functionality
@pytest.mark.parametrize("said, decision, confirmed", [("Oui", "oui", True), ("Non, pas ce compte", "non", False)])
def test_an_account_to_confirm_is_applied_on_a_clear_oui_only(said, decision, confirmed, monkeypatch):
    seen = []
    monkeypatch.setattr(
        pipeline, "confirm_account", lambda i, account, yes: seen.append((i, account, yes)) or ok("fait")
    )
    item = {
        "invoice_id": 272471,
        "step": "assign_account",
        "question": "q",
        "choices": {"245390": "61425000 Transport"},
    }
    pipeline.apply_answer(item, said, decision)
    assert seen == [(272471, 245390, confirmed)]
