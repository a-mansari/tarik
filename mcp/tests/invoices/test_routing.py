"""Where a message goes: an answer, the files, or both, from the database and not from the tool the agent called."""

import json
from pathlib import Path

import pytest

from tarik.core.cabinets import cabinet_db
from tarik.invoices import pipeline

WORKFLOW = Path(__file__).resolve().parents[3] / "workflows" / "tarik-traitement.json"


@pytest.fixture
def state(monkeypatch):
    """route_message over a conversation that waits on the client (or not), with files that came (or not)."""
    state = {"waiting": [], "files": 0}
    monkeypatch.setattr(pipeline, "waiting_on", lambda session_id: state["waiting"])
    monkeypatch.setattr(pipeline, "new_files", lambda session_id: state["files"])
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
    nodes = json.loads(WORKFLOW.read_text())
    connections = nodes["connections"]["Fichiers ou reponse ?"]["main"]
    names = [{target["node"] for target in output} for output in connections]
    assert len(names) == 4
    assert names[1] == {"Reponse du client (answer_pending)"} and names[0] == {"Dossier (resolve_dossier)"}
    assert names[3] == names[0] | names[1]  # both: the files and the answer, the summary tells all


@pytest.mark.functionality
def test_a_message_goes_where_the_database_says_unless_the_routing_failed():
    switch = next(n for n in json.loads(WORKFLOW.read_text())["nodes"] if n["name"] == "Fichiers ou reponse ?")
    expression = switch["parameters"]["output"]
    assert "$('Aiguillage (route_message)').first().json.content?.[0]?.text?.route" in expression
    assert "?? $('Appel').first().json.mode" in expression  # the tool the agent called, when the routing failed
