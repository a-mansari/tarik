"""The client's answers to what a conversation waits on, on the real Postgres (db_cabinet)."""

import pytest

from engine.tools.core.app import ok, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.conversation import yes_no
from engine.tools.invoices import exercice, intake, pipeline
from engine.tools.invoices.pipeline import made_in_experio


@pytest.fixture(autouse=True)
def none_made_in_experio(monkeypatch):
    """The dossier's drafts people made in Experio: none, but in the tests of made_in_experio itself."""
    monkeypatch.setattr(pipeline, "made_in_experio", lambda dossier_id: set())


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
    assert pipeline.answer_pending("s", "oui, TVA 20 %", "oui")["status"] == "question" and answers == []  # which?
    for about in ("99", "FA-20 ou FA-35"):  # one not waiting, not one: its draft is taken up, no question answered
        assert pipeline.answer_pending("s", "oui, TVA 20 %", "oui", about)["next_step"] == "waiting_drafts"
        assert answers == []
    assert pipeline.answer_pending("s", "oui pour la facture 35", "oui", "facture 35")["invoice_id"] == 35


@pytest.mark.security
def test_an_answer_about_another_invoice_never_takes_the_only_question(answers):
    """« 1580156068 : non. IFS a le numéro 2503-0702 »: the agent's call about IFS left SRM's question alone."""
    waits(11, "settle_prepaid", "Facture n° 1580156068 (SRM) : la répartir en charges constatées d'avance ?")
    result = pipeline.answer_pending(
        "s", "IFS NEUTRAL MARITIME a le numéro 2503-0702", "", "IFS NEUTRAL MARITIME MAROC"
    )
    assert result["next_step"] == "waiting_drafts" and "pour « IFS NEUTRAL MARITIME MAROC »" in result["message"]
    assert answers == []


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
def test_the_bank_a_dossier_without_bank_journal_is_told_goes_to_the_import_as_the_clients_words(answers, monkeypatch):
    with cabinet_db() as db:
        file_id = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind) values "
            "('s', 'mars.pdf', 'application/pdf', '', 'statement') returning id"
        ).fetchone()[0]
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, dossier_id, step, status, message, choices) "
            "values ('r', 's', %s, 557, 'import_bank_statement', 'question', %s, %s)",
            (file_id, f"mars.pdf : pas de journal. {intake.BANK_QUESTION}", '{"2": "CIH", "5": "BANK OF AFRICA"}'),
        )
    calls = []
    monkeypatch.setattr(
        pipeline,
        "import_bank_statement",
        lambda file_id, dossier_id, journal_id, client_message="": (
            calls.append((journal_id, client_message)) or ok("x")
        ),
    )
    pipeline.answer_pending("s", "c'est la BMCE", "5")
    assert calls == [(0, "BANK OF AFRICA c'est la BMCE")]  # never the bank's id as a journal's


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
    assert exercice.settle_exercice(7, "du 15/03/2026")["message"].endswith("rattachée à l'exercice `Exercice 2026`.")
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
def test_a_draft_whose_latest_outcome_is_a_wait_is_taken_up_again_however_far_it_went_before(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain, **kw: domain[0][2])  # all still drafts
    for run, step, status in [
        ("r1", "hold_posting", "stop"),  # verified, kept unposted
        ("r2", "settle_withholding", "wait"),  # taken up since: its supplier's attestation is being read
    ]:
        pipeline.record_step(run, "s", step, {"status": status, "message": "m", "invoice_id": 6})
    assert pipeline.waiting_drafts("s")["invoices"] == [6]
    pipeline.record_step("r3", "s", "settle_withholding", {"status": "question", "message": "m", "invoice_id": 6})
    assert pipeline.waiting_drafts("s")["invoices"] == []  # now it waits on the client
    pipeline.record_step("r4", "s", "configure_cabinet", {"status": "stop", "message": "m", "invoice_id": 6})
    assert pipeline.waiting_drafts("s")["invoices"] == []  # a setting stopped it: not on a follow-up...
    assert pipeline.waiting_drafts("s", stopped=True)["invoices"] == [6]  # ...but when the client says to resume


@pytest.mark.communication
def test_nothing_left_to_post_is_not_said_when_the_same_run_just_posted(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain, **kw: [])  # no draft left
    pipeline.record_step("r", "s", "post_invoice", {"status": "ok", "message": "m", "invoice_id": 6})
    assert pipeline.waiting_drafts("s", held=True, run_id="r")["routine"]  # « comptabilisez » went both ways
    assert pipeline.waiting_drafts("s", held=True, run_id="r2")["status"] == "stop"  # a run that posted nothing


@pytest.mark.functionality
def test_a_run_waits_on_experio_reading_once_for_all_its_drafts(monkeypatch):
    clock, states = [0.0], {1: "pending", 2: "pending", 3: "done"}
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    now = pipeline.datetime.now(pipeline.UTC).replace(tzinfo=None).isoformat()  # drafts read for a minute: not stuck
    monkeypatch.setattr(
        pipeline,
        "require_invoice",
        lambda invoice_id, fields: {
            "bluejay_state": states[invoice_id],
            "state": "pending",
            "create_date": now,
            "write_date": now,
        },
    )
    monkeypatch.setattr(pipeline, "READ_DEADLINES", {})
    monkeypatch.setattr(pipeline, "split_off", lambda invoice_id: [])
    monkeypatch.setattr(pipeline, "resend_if_stuck", lambda invoice_id: "")
    assert pipeline.wait_until_read(1, "run")["status"] == "wait"
    waited = clock[0]
    assert pipeline.READ_WAIT <= waited < pipeline.READ_WAIT + 15  # one stuck reading never holds the run
    assert pipeline.wait_until_read(2, "run")["status"] == "wait" and clock[0] == waited  # the deadline is passed
    assert pipeline.wait_until_read(3, "run")["status"] == "ok"
    assert pipeline.wait_until_read(2, "next run")["status"] == "wait" and clock[0] > waited  # its own deadline


@pytest.mark.functionality
def test_a_reading_stuck_over_an_hour_is_sent_again_once_an_hour(monkeypatch):
    calls, now = [], pipeline.datetime.now(pipeline.UTC).replace(tzinfo=None)
    draft = {"state": "pending", "company_id": [5, "D"]}

    def at(minutes):
        return (now - pipeline.timedelta(minutes=minutes)).isoformat(sep=" ", timespec="seconds")

    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: draft)
    monkeypatch.setattr(pipeline, "dry_run", lambda: False)
    monkeypatch.setattr(pipeline, "odoo_json", lambda model, method, ids, context: calls.append((method, ids)))
    draft.update(create_date=at(90), write_date=at(70))
    assert "relire" in pipeline.resend_if_stuck(4) and calls == [("action_resend", [4])]
    draft.update(write_date=at(5))  # sent again 5 minutes ago (the resend touches it): not again yet
    assert pipeline.resend_if_stuck(4) == "" and len(calls) == 1
    draft.update(create_date=at(30), write_date=at(30))  # under an hour: Experio's own threshold not reached
    assert pipeline.resend_if_stuck(4) == "" and len(calls) == 1
    draft.update(state="draft", create_date=at(90), write_date=at(90))  # read since: nothing to resend
    assert pipeline.resend_if_stuck(4) == "" and len(calls) == 1


@pytest.mark.functionality
def test_bank_journals_asked_together_wait_one_by_one_and_are_answered_together(db_cabinet, monkeypatch):
    pipeline.record_step(
        "r",
        "s",
        "ask_banks",
        {
            "status": "question",
            "message": "Dossier Gecafisc : à quelle banque est chacun de ces journaux ?\n- `BQ1` :\n- `BQ2` :",
            "dossier_id": 557,
            "journal_id": 11,
            "journal_ids": [11, 12],
            "choices": {3: "CIH", 5: "BP"},
        },
    )
    unbanked = {11: "BQ1", 12: "BQ2"}
    monkeypatch.setattr(pipeline, "journal_name", lambda journal_id: unbanked.get(journal_id))
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id, invoice_id: {"status": "ok"})
    monkeypatch.setattr(pipeline, "odoo_call", lambda *a, **k: [])
    answered = []

    def answer_banks(dossier_id, answer, decision, choices):
        answered.append((dossier_id, answer, decision, choices))
        return ok("Dossier Gecafisc, banque des journaux : …")

    monkeypatch.setattr(pipeline, "answer_banks", answer_banks)
    waiting = pipeline.waiting_on("s")
    assert [item["label"] for item in waiting] == ["journal BQ1", "journal BQ2"]  # one question each
    result = pipeline.answer_pending("s", "BQ1 cih, BQ2 banque populaire")  # no « about »: both at once
    assert result["step"] == "ask_banks" and answered == [
        (557, "BQ1 cih, BQ2 banque populaire", "", {"3": "CIH", "5": "BP"})
    ]
    unbanked.pop(11)  # given its bank since
    assert [item["label"] for item in pipeline.waiting_on("s")] == ["journal BQ2"]
    pipeline.record_step("r", "s", "answer_pending", {"status": "question", "step": "ask_banks", "journal_ids": [12]})
    assert [item["label"] for item in pipeline.waiting_on("s")] == ["journal BQ2"]  # a row with no dossier: no more
    pipeline.record_step(
        "r", "s", "ask_banks", {"status": "ok", "message": "…", "dossier_id": 557, "settled_ids": [12]}
    )
    assert pipeline.waiting_on("s") == []  # closed by its own row, even with no bank in Experio (test mode)


@pytest.mark.functionality
def test_an_invoice_stays_a_verified_draft_until_the_client_says_to_post_it(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])  # none in this dossier
    searched = []

    def search(model, method, domain, **kw):
        if method == "search_read" or domain[0][1] == "not in":  # the dossier's drafts left: none
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
    assert ("company_id", "in", [5]) in searched[-2]  # the last search: which of them are transit invoices
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
    assert ("company_id", "in", [5]) in searched[-2]  # the last search: which of them are transit invoices
    assert pipeline.waiting_drafts("new", stopped=True)["invoices"] == []  # no dossier: nothing from elsewhere


@pytest.mark.functionality
def test_a_draft_tarik_posted_and_experio_shows_as_draft_again_is_posted_again(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])  # none in this dossier
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
    assert "Facture 1 (F-1) attend votre réponse à cette question : quel compte ?" in told["message"]
    assert "- Facture 2 (F-2) : doublon." in told["message"]
    assert "Facture 3 (sans numéro) n'a pas été importée par moi" in told["message"]


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
    assert "Facture 2 (F-2) est en cours de traitement" in told and "brouillon 2 créé" not in told


@pytest.mark.communication
def test_an_earlier_runs_stop_is_not_said_of_a_draft_another_run_took_up_again(db_cabinet, monkeypatch):
    drafts = [{"id": 2, "invoice_number": "F-2"}]
    monkeypatch.setattr(
        pipeline, "odoo_call", lambda model, method, domain, **kw: drafts if method == "search_read" else []
    )
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) values "
            "('s', 'b.pdf', 'application/pdf', '', 2)"
        )
    pipeline.record_step(  # an earlier run held it at its draft, furthest in the sequence
        "A", "s", "hold_posting", {"status": "stop", "message": "laissée en brouillon", "invoice_id": 2}
    )
    with cabinet_db() as db:
        db.execute("delete from tarik_working where run_id = 'A'")  # run A ended: its claim released
    pipeline.record_step(  # the same file sent again in another thread: run C took it up from its reading
        "C", "other", "wait_until_read", {"status": "ok", "message": "lue par Experio", "invoice_id": 2}
    )
    told = pipeline.waiting_drafts("s", held=True, dossier_id=5, run_id="B")["message"]
    assert "Facture 2 (F-2) est en cours de traitement" in told and "laissée en brouillon" not in told


@pytest.mark.functionality
def test_posting_takes_the_conversations_own_held_drafts_first(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])  # none in this dossier
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
    assert "2 brouillons d'autres conversations ou créés dans Experio attendent" in told
    assert "« comptabilisez toutes les factures du dossier »" in told
    assert pipeline.waiting_drafts(
        "new", held=True, dossier_id=5, client_message="Comptabilisez toutes les factures du dossier."
    )["invoices"] == [7, 8]


@pytest.mark.security
def test_drafts_of_other_conversations_are_taken_only_when_the_client_names_them(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])  # none in this dossier
    drafts = {7: ("F-77", [1, "MIDAS - 4411M005"]), 8: ("AB-2026-12", [2, "EXELENTIA INTERNATIONAL TRANS"])}

    def odoo_call(model, method, domain, **kw):
        if method == "search_read" and domain[0][0] == "id":
            return [{"id": i, "invoice_number": drafts[i][0], "partner_id": drafts[i][1]} for i in domain[0][2]]
        if domain[0][1] == "not in":
            return []  # nothing else left a draft in the dossier
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


@pytest.mark.functionality
def test_a_held_draft_of_the_conversation_is_posted_though_its_hold_was_recorded_in_another(db_cabinet, monkeypatch):
    # the same file sent in two threads: the hold kept under the first, the client asks to post in the second
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])

    def odoo_call(model, method, domain, **kw):
        if method == "search_read":  # the names a client may call a draft by: none here
            return [{"id": i, "invoice_number": "", "partner_id": False} for i in domain[0][2]]
        return [] if domain[0][1] == "not in" else domain[0][2]

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    pipeline.hold_posting(4, "first")
    pipeline.record_step(
        "R", "s", "hold_posting", {"status": "stop", "message": "laissée en brouillon", "invoice_id": 4}
    )
    assert pipeline.waiting_drafts("s", held=True, dossier_id=5, client_message="comptabilisez la")["invoices"] == [4]


@pytest.mark.communication
def test_asked_for_the_whole_dossier_what_stays_a_draft_is_said_with_why(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])
    left = [21, 22, 23, 24]

    def odoo_call(model, method, domain, **kw):
        return left if domain[0][1] == "not in" else domain[0][2]

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) values "
            "('x', 'a.pdf', 'application/pdf', '', 21), ('x', 'b.pdf', 'application/pdf', '', 22)"
        )
        db.execute(
            "insert into tarik_pending (invoice_id, session_id, step, question) values "
            "(21, 'x', 'assign_account', 'quel compte ?')"
        )
    pipeline.hold_posting(4, "s")
    told = pipeline.waiting_drafts(
        "s", held=True, dossier_id=5, client_message="comptabilisez toutes les factures du dossier"
    )
    assert told["invoices"] == [4] and told["tell"]
    assert told["message"] == (
        "Restent en brouillon dans ce dossier :\n"
        "- 1 facture attend votre réponse à sa question, dans sa conversation.\n"
        "- 1 facture est arrêtée par un point à corriger.\n"
        "- 2 factures n'ont pas été importées par moi : comptabilisez-les dans Experio."
    )


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


@pytest.mark.security
def test_an_account_given_to_the_processing_tool_goes_only_to_the_draft_named(three_drafts):
    said = "Objet : Factures\n\nMettez la 272471 en compte 61425000, s'il vous plaît."
    assert pipeline.drafts_corrected("s", said, "61425000") == [[272471, "61425000"]]
    assert pipeline.drafts_corrected("s", "Mettez-les en 61425000.", "61425000") == []  # none named: none changed
    assert pipeline.drafts_corrected("s", "La 272471 et la 272469 en 61425000.", "61425000") == []  # which one?


@pytest.mark.functionality
def test_facturation_drafts_are_posted_only_when_the_client_asks_for_them(db_cabinet, monkeypatch):
    """Drafts Experio's Facturation made: never read by Tarik, taken up (at the dossier's settings) only on the
    client's own words, the whole dossier or by name."""
    monkeypatch.setattr(pipeline, "experio_form", lambda: {"types": {"gst_invoice_id": "many2one"}})
    billed = [{"id": 81, "invoice_number": "FV-2026-12", "partner_id": [9, "CLIENT SA"]}]

    def odoo_call(model, method, domain, **kw):
        if method == "search_read":
            return [b for b in billed if b["id"] in domain[0][2]]
        if ("gst_invoice_id", "!=", False) in domain:
            return [81]
        return [i for i in domain[0][2] if i != 81]  # Tarik's own: none

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    assert pipeline.waiting_drafts("s", held=True, dossier_id=5, client_message="Comptabilisez.")["invoices"] == []
    asked = pipeline.waiting_drafts("s", held=True, dossier_id=5, client_message="Comptabilisez la FV-2026-12.")
    assert asked["facturation"] == [81] and asked["invoices"] == []
    whole = "Comptabilisez toutes les factures du dossier."
    assert pipeline.waiting_drafts("s", held=True, dossier_id=5, client_message=whole)["facturation"] == [81]
    assert "facturation" not in pipeline.waiting_drafts("s", dossier_id=5, client_message=whole)  # not asked to post


@pytest.mark.functionality
def test_a_message_naming_invoices_takes_up_those_only(db_cabinet, monkeypatch):
    """« comptabilisez la facture 112 » posts the 112, not every draft the conversation holds."""
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])
    drafts = {
        112: {"id": 112, "invoice_number": "AB-12", "partner_id": [3, "ATLAS - 4411"]},
        113: {"id": 113, "invoice_number": "CD-13", "partner_id": False},
    }

    def odoo_call(model, method, domain, **kw):
        ids = [i for i in domain[0][2] if i in drafts]
        return [drafts[i] for i in ids] if method == "search_read" else ids

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    for invoice_id in drafts:
        pipeline.hold_posting(invoice_id, "s")
        held = {"status": "stop", "message": "laissée en brouillon", "invoice_id": invoice_id}
        pipeline.record_step("R", "s", "hold_posting", held)
    both = pipeline.waiting_drafts("s", held=True, dossier_id=5, client_message="comptabilisez-les")
    assert both["invoices"] == [112, 113]
    one = pipeline.waiting_drafts("s", held=True, dossier_id=5, client_message="comptabilisez la facture 112")
    assert one["invoices"] == [112]
    atlas = pipeline.waiting_drafts("s", held=True, dossier_id=5, client_message="comptabilisez celle d'Atlas")
    assert atlas["invoices"] == [112]  # by its supplier too


@pytest.mark.functionality
def test_a_file_refused_as_no_document_is_not_taken_up_again_unless_named(db_cabinet):
    """The Orange receipt was refused once: it must not come back at every message and keep the drafts from being
    posted; « le recu orange est une facture » names it."""
    from engine.tools.core.cabinets import cabinet_db

    with cabinet_db() as db:
        for name, kind in (("2025-04-07_purchase_75552.pdf", "other"), ("facture_atlas.pdf", "invoice")):
            db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, kind) "
                "values ('s-receipt', %s, 'application/pdf', %s, %s)",
                (name, b"%PDF", kind),
            )
    listed = pipeline.files_to_process("s-receipt", client_message="comptabilisez les factures prêtes")
    assert len(listed["files"]) == 1  # the invoice only
    named = pipeline.files_to_process("s-receipt", client_message="2025-04-07_purchase_75552.pdf est une facture")
    assert len(named["files"]) == 2


@pytest.mark.functionality
def test_a_suppliers_answer_goes_to_each_of_its_invoices_asking_the_same_thing():
    asking = {"step": "settle_withholding", "names": {"azur transit", "azur"}}
    waiting = [
        {**asking, "invoice_id": 1, "names": {"1", "1297", "azur transit"}},
        {**asking, "invoice_id": 2, "names": {"2", "1323", "azur transit"}},
        {"step": "settle_tva", "invoice_id": 3, "names": {"3", "azur transit"}},  # another question of the supplier
        {"step": "settle_withholding", "invoice_id": 4, "names": {"4", "gecafisc"}},
    ]
    assert [i["invoice_id"] for i in pipeline.meant_all(waiting[:2] + waiting[3:], "azur transit")] == [1, 2]
    assert pipeline.meant_all(waiting, "azur transit") == []  # two different questions: which one?
    assert [i["invoice_id"] for i in pipeline.meant_all(waiting, "1297")] == [1]  # by its own number: that one
    assert [i["invoice_id"] for i in pipeline.meant_all(waiting, "gecafisc")] == [4]
    prepaid = [{"step": "settle_prepaid", "invoice_id": i, "names": {str(i), "srm"}} for i in (5, 6)]
    assert pipeline.meant_all(prepaid, "srm") == []  # a per-invoice question is never answered for the others
    assert pipeline.meant(waiting[:2], "azur transit") is None  # several: not one item


@pytest.mark.functionality
def test_a_yes_to_a_new_account_creates_it_and_a_yes_to_an_account_confirms_it(monkeypatch):
    made = []
    monkeypatch.setattr(pipeline, "create_account", lambda i, parent, label, yes: made.append(("new", i, parent, yes)))
    monkeypatch.setattr(pipeline, "confirm_account", lambda i, a, yes: made.append(("confirm", i, a, yes)))
    monkeypatch.setattr(pipeline, "set_account", lambda i, answer, choices=None: made.append(("set", i, choices)))
    item = {"step": "assign_account", "invoice_id": 5, "choices": {"new 71240000": "71242000 Terrassement"}}
    pipeline.answer_invoice(item, "oui", "")
    pipeline.answer_invoice({**item, "choices": {"2": "71241000 Travaux"}}, "ok", "oui")
    several = {"2": "71241000 Travaux", "3": "71243000 Prestations de services"}
    pipeline.answer_invoice({**item, "choices": several}, "le deuxième", "")
    assert made == [("new", 5, "71240000", True), ("confirm", 5, 2, True), ("set", 5, several)]


@pytest.mark.functionality
def test_the_drafts_people_made_in_experio_are_those_tarik_never_made_worked_on_or_asked_about(db_cabinet, monkeypatch):
    asked = []
    monkeypatch.setattr(pipeline, "experio_form", lambda: {"types": {"gst_invoice_id": "many2one"}})
    monkeypatch.setattr(
        pipeline, "odoo_call", lambda model, method, domain: asked.append(domain) or [30, 31, 32, 33, 34]
    )
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) values "
            "('s', 'a.pdf', 'application/pdf', '', 30)"
        )
        db.execute("insert into tarik_pending (invoice_id, session_id, step, question) values (31, 's', 'x', 'q')")
    pipeline.record_step("r", "s", "check_invoice", {"status": "ok", "message": "m", "invoice_id": 32})
    assert made_in_experio(5) == {33, 34}
    assert asked == [[("company_id", "=", 5), ("state", "in", ["pending", "draft"]), ("gst_invoice_id", "=", False)]]
    monkeypatch.setattr(pipeline, "experio_form", lambda: {"types": {}})  # no Facturation in this Experio
    assert made_in_experio(5) == {33, 34} and asked[-1] == asked[0][:2]


@pytest.mark.security
def test_the_drafts_people_made_in_experio_are_taken_only_when_the_client_asks_for_the_dossier(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])
    monkeypatch.setattr(pipeline, "made_in_experio", lambda dossier_id: {50, 51, 52})
    NAMES = {50: "ALPHA TRANSPORT", 51: "BRAVO LOGISTIQUE", 52: "CHARLIE SERVICES"}

    def odoo_call(model, method, domain, **kw):
        if method == "search_read":
            return [{"id": i, "invoice_number": f"X-{i}", "partner_id": [i, NAMES[i]]} for i in domain[0][2]]
        return [] if domain[0][1] == "not in" else domain[0][2]

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    told = pipeline.waiting_drafts("new", held=True, dossier_id=5, client_message="Comptabilisez les factures.")
    assert told["invoices"] == []
    assert "3 brouillons d'autres conversations ou créés dans Experio attendent" in told["message"]
    assert (
        pipeline.waiting_drafts("new", held=True, client_message="Comptabilisez toutes les factures.")["invoices"] == []
    )
    for said, taken in [
        ("Comptabilisez toutes les factures du dossier.", [50, 51, 52]),
        ("Comptabilisez la X-51.", [51]),
        ("Comptabilisez celle de Charlie.", [52]),
    ]:
        assert pipeline.waiting_drafts("new", held=True, dossier_id=5, client_message=said)["invoices"] == taken, said
    # Only when the client asks to post: reading drafts again never takes what people made.
    assert (
        pipeline.waiting_drafts("new", stopped=True, dossier_id=5, client_message="Reprenez toutes les factures")[
            "invoices"
        ]
        == []
    )


@pytest.mark.functionality
def test_no_more_than_so_many_drafts_made_in_experio_are_taken_at_once_and_the_rest_is_said(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "facturation_drafts", lambda dossier_id, words: [])
    monkeypatch.setattr(pipeline, "AT_ONCE", 2)
    monkeypatch.setattr(pipeline, "made_in_experio", lambda dossier_id: {50, 51, 52})
    remaining = [52]

    def odoo_call(model, method, domain, **kw):
        return remaining if domain[0][1] == "not in" else domain[0][2]

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    told = pipeline.waiting_drafts(
        "new", held=True, dossier_id=5, client_message="Comptabilisez toutes les factures du dossier."
    )
    assert told["invoices"] == [50, 51] and told["tell"]
    assert told["message"] == (
        "Restent en brouillon dans ce dossier :\n"
        "- 1 facture créée dans Experio n'a pas été reprise cette fois (2 au plus à la fois) : "
        "redemandez-moi de la reprendre."
    )
