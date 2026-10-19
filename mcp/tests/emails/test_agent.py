"""Asking the cabinet's agent for an email thread's reply."""

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core import cabinets
from tarik.emails import agent
from tests.fakes import FakeDb


@pytest.fixture
def cabinet(monkeypatch):
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"tarik_test_a"}))
    monkeypatch.setenv("TARIK_TEST_A__AGENT_ID", "agent-of-a")
    with cabinets.using("tarik_test_a"):
        yield


@pytest.fixture(autouse=True)
def no_memory(monkeypatch):
    monkeypatch.setattr(agent, "remember_client_message", lambda *args: None)
    monkeypatch.setattr(agent, "untaken", lambda session_id: 0)  # the files were taken: asked once


@pytest.mark.security
def test_the_thread_goes_to_its_cabinets_agent_in_its_own_conversation(cabinet, monkeypatch):
    sent = {}
    monkeypatch.setattr(agent, "cabinet_db", lambda: FakeDb((1,)))
    monkeypatch.setattr(
        agent,
        "call_n8n",
        lambda tool, **kw: (
            sent.update(tool=tool, **kw) or {"ok": True, "status": "completed", "response": "Bien recu."}
        ),
    )
    assert agent.ask_agent("mail-1", "Voici la facture", ["f.pdf", "g.pdf"])["output"] == "Bien recu."
    assert sent == {
        "tool": "call_agent",
        "agentId": "agent-of-a",
        "request": {
            "type": "message",
            "message": "Voici la facture\n\nFichiers joints : f.pdf, g.pdf",
            "sessionId": "mail-1",
            "newSession": True,
        },
    }


@pytest.mark.security
def test_only_a_known_email_conversation(cabinet, monkeypatch):
    monkeypatch.setattr(agent, "cabinet_db", lambda: FakeDb())
    monkeypatch.setattr(agent, "call_n8n", lambda *a, **k: pytest.fail("n8n called"))
    with pytest.raises(ToolError, match="inconnue"):
        agent.ask_agent("mail-of-someone-else", "x")


@pytest.mark.communication
@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "code": "session_not_found", "message": "Session not found."},
        {"ok": True, "status": "suspended", "response": "?"},
        {"ok": True, "status": "completed"},
    ],
)
def test_no_reply_is_an_error_without_n8n_details(cabinet, monkeypatch, answer):
    monkeypatch.setattr(agent, "cabinet_db", lambda: FakeDb((1,)))
    monkeypatch.setattr(agent, "call_n8n", lambda *a, **k: answer)
    with pytest.raises(ToolError) as error:
        agent.ask_agent("mail-1", "x")
    assert str(error.value) == "Tarik n'a pas pu repondre pour le moment."


@pytest.mark.security
def test_the_files_names_are_never_the_clients_words():
    from tarik.core.conversation import own_words

    assert own_words("Voici la facture\n\nFichiers joints : ICE 001122334455667.pdf") == "Voici la facture"
    assert own_words("(pas de message)\n\nFichiers joints : f.pdf") == "" and own_words(None) == ""


@pytest.mark.technical
@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_n8n_s_answer_is_read_by_its_shape_whatever_the_reply_says(monkeypatch, content_type):
    reply = {"response": "Les metadata: et data:image/png sont dans le fichier."}  # words the old check misread
    body = json.dumps({"result": {"structuredContent": reply}})
    text = f"event: message\ndata: {body}\n\n" if content_type == "text/event-stream" else body
    answer = httpx.Response(200, text=text, headers={"content-type": content_type}, request=httpx.Request("POST", "u"))
    monkeypatch.setenv("N8N_MCP_API_URL", "http://n8n/mcp")
    monkeypatch.setattr(agent, "setting", lambda key: "token")
    monkeypatch.setattr(agent.httpx, "post", lambda *args, **kwargs: answer)
    assert agent.call_n8n("call_agent") == reply


@pytest.fixture
def asked(cabinet, monkeypatch):
    """ask_agent for files: the messages the agent got, and how many files stay untaken after each ask."""
    sent, left = [], []
    monkeypatch.setattr(agent, "cabinet_db", lambda: FakeDb((1,)))

    def call_n8n(tool, **kw):
        sent.append(kw["request"]["message"])
        return {"ok": True, "status": "completed", "response": f"reponse {len(sent)}"}

    monkeypatch.setattr(agent, "call_n8n", call_n8n)
    monkeypatch.setattr(agent, "untaken", lambda session_id: left.pop(0))
    return sent, left


@pytest.mark.functionality
def test_files_left_untaken_remind_the_agent_until_it_takes_them(asked):
    sent, left = asked
    left += [2, 0]  # the first answer took none, the second took them
    assert agent.ask_agent("mail-1", "Voici les achats de KTSYS", ["f.pdf"])["output"] == "reponse 2"
    assert len(sent) == 2 and sent[1].startswith(sent[0]) and "Rappel" in sent[1] and "Rappel" not in sent[0]


@pytest.mark.functionality
def test_the_agent_is_asked_at_most_a_set_number_of_times(asked):
    sent, left = asked
    left += [2] * agent.ASK_TRIES
    assert agent.ask_agent("mail-1", "x", ["f.pdf"])["output"] == f"reponse {agent.ASK_TRIES}"
    assert len(sent) == agent.ASK_TRIES and sent[1] == sent[2]  # the reminder is not stacked


@pytest.mark.security
def test_the_reminder_is_never_the_clients_words(asked):
    from tarik.core.conversation import own_words

    sent, left = asked
    left += [1, 0]
    agent.ask_agent("mail-1", "Voici les achats", ["f.pdf"])
    assert [own_words(text) for text in sent] == ["Voici les achats"] * 2


@pytest.mark.functionality
def test_no_file_no_check_and_a_slow_ask_is_not_repeated(asked, monkeypatch):
    sent, left = asked
    monkeypatch.setattr(agent, "untaken", lambda session_id: pytest.fail("checked without a file"))
    agent.ask_agent("mail-1", "Une question", None)
    assert len(sent) == 1
    sent.clear()
    monkeypatch.setattr(agent, "untaken", lambda session_id: 3)
    clock = iter([0, agent.QUICK + 1])
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(clock))
    agent.ask_agent("mail-1", "x", ["f.pdf"])
    assert len(sent) == 1  # a tool cut at 5 minutes may still run: never a second run beside it
