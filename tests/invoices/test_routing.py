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
    "compte, corrected, sent_to",
    [
        ("", None, [(7, "check_journal", "")]),  # the answer's own next step
        ("frais de péage", 7, [(7, "assign_account", "frais de péage")]),  # its own account corrected: back to it
        # Another invoice's account: that one goes back to its account step, the answered one goes on without it.
        ("61425000", 9, [(7, "check_journal", ""), (9, "assign_account", "61425000")]),
    ],
)
def test_an_account_corrected_with_an_answer_goes_to_the_invoice_it_names(compte, corrected, sent_to):
    from engine import traitement

    last = {"status": "ok", "next_step": "check_journal", "invoice_id": 7, "corrected_invoice": corrected}
    state = {"call": {"session_id": "s", "compte": compte}, "run_id": "r", "dossier_id": 5, "outcomes": [last]}
    sends = traitement.resume_at(state)
    assert [(s.arg["invoice_id"], s.arg["entry"], s.arg["call"]["compte"]) for s in sends] == sent_to


@pytest.mark.communication
def test_a_message_without_new_files_asks_only_its_own_conversation_s_questions():
    from engine import traitement

    # Never the questions other conversations left on the dossier: straight to this conversation's drafts.
    assert traitement.each_file({"files": [], "outcomes": [{"status": "ok"}]}) == "brouillons"
    assert "questions" not in traitement.graph.get_graph().nodes
