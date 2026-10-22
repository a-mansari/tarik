"""Where a message goes: an answer, the files, or both, from the database and not from the tool the agent called."""

import pytest

from tarik.core.cabinets import cabinet_db
from tarik.invoices import pipeline


@pytest.fixture
def state(monkeypatch):
    """route_message over a conversation that waits on the client (or not), with files that came (or not)."""
    state = {"waiting": [], "files": 0, "uncut": 0}
    monkeypatch.setattr(pipeline, "waiting_on", lambda session_id: state["waiting"])
    monkeypatch.setattr(pipeline, "new_files", lambda session_id: state["files"])
    monkeypatch.setattr(pipeline, "awaiting_split", lambda session_id: state["uncut"])
    return state


@pytest.mark.functionality
@pytest.mark.parametrize(
    "waiting, files, mode, route",
    [
        (True, 0, "files", "answer"),  # the agent chose processing for what is an answer
        (True, 0, "answer", "answer"),
        (False, 2, "answer", "files"),  # the agent chose answering for new files
        (False, 2, "files", "files"),
        (True, 2, "files", "both"),  # the catch: an answer and new files in one message
        (True, 2, "answer", "both"),
        (False, 0, "files", "files"),  # nothing waits, nothing new: the drafts and files left are picked up
        (False, 0, "answer", "answer"),  # the drafts Experio had not read are taken up again
        (True, 2, "resume", "resume"),  # a follow-up has no message to route
        (False, 0, "other", "files"),
    ],
)
def test_the_database_decides_where_a_message_goes(state, waiting, files, mode, route):
    state["waiting"], state["files"] = [{"invoice_id": 11}] if waiting else [], files
    assert pipeline.route_message("s", mode)["route"] == route


@pytest.mark.functionality
def test_how_to_cut_a_pdf_answered_goes_to_its_files(state):
    state["uncut"] = 1  # a PDF of several pages asked about: « par page » is for it, whatever tool the agent took
    assert pipeline.route_message("s", "answer")["route"] == "files"
    state["waiting"] = [{"invoice_id": 11}]
    assert pipeline.route_message("s", "answer")["route"] == "both"


@pytest.mark.functionality
def test_files_are_new_only_since_the_conversations_last_run(db_cabinet):
    def file(name, created, **columns):
        with cabinet_db() as db:
            db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, created_at, invoice_id, kind) "
                "values ('s', %s, 'application/pdf', ''::bytea, %s, %s, %s)",
                (name, created, columns.get("invoice_id"), columns.get("kind")),
            )

    file("facture.pdf", "2026-01-01 10:00")
    assert pipeline.new_files("s") == 1  # no run yet
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_results (run_id, session_id, step, status, message, created_at) "
            "values ('1', 's', 'link_tiers', 'question', 'x', '2026-01-01 10:05')"
        )
    assert pipeline.new_files("s") == 0  # it came before the last run: the answer to its question is not a new file
    file("suite.pdf", "2026-01-01 10:10")
    file("deja.pdf", "2026-01-01 10:10", invoice_id=5)  # already a draft
    file("ecritures.xlsx", "2026-01-01 10:10")  # a dossier's entries go to the other workflow
    file("fec.pdf", "2026-01-01 10:10", kind="entries")
    assert pipeline.new_files("s") == 1  # only suite.pdf
    assert pipeline.new_files("another") == 0


@pytest.mark.functionality
def test_a_message_with_an_answer_and_new_files_does_both_in_one_run():
    from engine import traitement

    assert traitement.by_route({"route": "answer"}) == "reponse"
    assert traitement.by_route({"route": "files"}) == "dossier"
    assert traitement.by_route({"route": "both"}) == ["reponse", "dossier"]  # the summary tells all


@pytest.mark.functionality
def test_a_message_goes_where_the_database_says_unless_the_routing_failed(monkeypatch):
    import asyncio

    from engine import traitement

    async def route(cabinet, tool, **arguments):
        return {"route": "both"}

    async def broken(cabinet, tool, **arguments):
        raise ConnectionError

    state = {"call": {"cabinet": "c", "session_id": "s", "mode": "answer"}}
    monkeypatch.setattr(traitement, "run_tool", route)
    assert asyncio.run(traitement.aiguillage(state)) == {"route": "both"}
    monkeypatch.setattr(traitement, "run_tool", broken)
    assert asyncio.run(traitement.aiguillage(state)) == {"route": "answer"}  # the tool the agent called


@pytest.mark.functionality
def test_asked_to_post_the_drafts_are_posted_whatever_question_waits(monkeypatch):
    import asyncio

    from engine import traitement

    async def route(cabinet, tool, **arguments):
        return {"route": "answer"}

    monkeypatch.setattr(traitement, "run_tool", route)
    state = {"call": {"cabinet": "c", "session_id": "s", "mode": "files", "comptabiliser": "oui"}}
    assert asyncio.run(traitement.aiguillage(state)) == {"route": "both"}
    state["call"]["comptabiliser"] = ""
    assert asyncio.run(traitement.aiguillage(state)) == {"route": "answer"}


@pytest.mark.functionality
@pytest.mark.parametrize(
    "corrections, sent_to",
    [
        ([], [(7, "check_journal", "")]),  # the answer's own next step
        ([[7, "frais de péage"]], [(7, "assign_account", "frais de péage")]),  # its own account: back to it
        # Another invoice's account: that one goes back to its account step, the answered one goes on without it.
        ([[9, "61425000"]], [(7, "check_journal", ""), (9, "assign_account", "61425000")]),
        ([[7, "6125"], [9, "61425000"]], [(7, "assign_account", "6125"), (9, "assign_account", "61425000")]),
    ],
)
def test_each_account_given_with_an_answer_goes_to_its_own_invoice(corrections, sent_to):
    from engine import traitement

    last = {"status": "ok", "next_step": "check_journal", "invoice_id": 7, "corrections": corrections}
    state = {"call": {"session_id": "s", "compte": "x"}, "run_id": "r", "dossier_id": 5, "outcomes": [last]}
    sends = traitement.resume_at(state)
    assert [(s.arg["invoice_id"], s.arg["entry"], s.arg["call"]["compte"]) for s in sends] == sent_to
    # Another invoice than the one answered: its account is to confirm; the answered one's is applied.
    assert all(s.arg["call"]["confirmer"] is (s.arg["invoice_id"] != 7) for s in sends)


@pytest.mark.functionality
def test_an_account_correction_goes_through_even_when_its_answer_settles_nothing():
    from engine import traitement

    # The agent sent the correction about 272471, whose own question (its exercice) the message does not answer.
    last = {"status": "question", "invoice_id": 272471, "corrections": [[272471, "61425000"]]}
    state = {"call": {"session_id": "s", "compte": ""}, "run_id": "r", "dossier_id": 5, "outcomes": [last]}
    (sent,) = traitement.resume_at(state)
    assert (sent.arg["invoice_id"], sent.arg["entry"], sent.arg["call"]["compte"]) == (
        272471,
        "assign_account",
        "61425000",
    )
    state["outcomes"] = [{"status": "question", "invoice_id": 272471}]
    assert traitement.resume_at(state) == "resume"  # no correction: as before


@pytest.mark.technical
def test_a_corrected_invoice_is_never_also_taken_up_with_the_waiting_drafts():
    from engine import traitement

    last = {"status": "ok", "next_step": "waiting_drafts", "invoice_id": None, "corrections": [[272470, "6125"]]}
    state = {"call": {"session_id": "s"}, "run_id": "r", "dossier_id": 5, "outcomes": [last]}
    fix, drafts = traitement.resume_at(state)
    assert (fix.node, fix.arg["invoice_id"]) == ("fichier", 272470)
    assert drafts.node == "brouillons" and drafts.arg["call"]["skip_invoices"] == [272470]


@pytest.mark.technical
def test_every_argument_the_agent_gives_the_processing_tools_reaches_the_workflow():
    from engine import agent, traitement

    # The workflow keeps only its INPUTS: an argument missing there is dropped before any step reads it.
    given = {
        name
        for tool in agent.TOOLS
        if tool.name in ("Experio_traiter_fichiers", "Experio_repondre")
        for name in tool.args
    }
    assert given - set(traitement.INPUTS) == set()


@pytest.mark.security
def test_drafts_taken_up_again_never_get_this_message_s_instructions():
    from engine import traitement

    call = {"session_id": "s", "compte": "61330000", "retenue": "sans", "comptabiliser": "oui"}
    state = {
        "call": call,
        "run_id": "r",
        "dossier_id": 5,
        "invoices": [272469, 272470],
        "corrections": [[272471, "61330000"]],
    }
    sends = {s.arg["invoice_id"]: (s.arg["entry"], s.arg["call"]) for s in traitement.each_draft(state)}
    for resumed in (272469, 272470):  # never the account the client gave 272471
        entry, given = sends[resumed]
        assert entry == "wait_until_read" and given["compte"] == "" and given["retenue"] == ""
        assert given["comptabiliser"] == "oui"  # « comptabilisez » is for them too
    assert sends[272471][0] == "assign_account" and sends[272471][1]["compte"] == "61330000"
    assert sends[272471][1]["confirmer"] is True
