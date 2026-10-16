"""Asking the cabinet's agent for an email thread's reply."""

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


@pytest.mark.security
def test_the_thread_goes_to_its_cabinets_agent_in_its_own_conversation(cabinet, monkeypatch):
    sent = {}
    monkeypatch.setattr(agent, "cabinet_db", lambda: FakeDb((1,)))
    monkeypatch.setattr(agent, "call_n8n", lambda tool, **kw: sent.update(tool=tool, **kw) or
                        {"ok": True, "status": "completed", "response": "Bien recu."})
    assert agent.ask_agent("mail-1", "Voici la facture", ["f.pdf", "g.pdf"])["output"] == "Bien recu."
    assert sent == {"tool": "call_agent", "agentId": "agent-of-a", "request": {
        "type": "message", "message": "Voici la facture\n\nFichiers joints : f.pdf, g.pdf", "sessionId": "mail-1", "newSession": True}}


@pytest.mark.security
def test_only_a_known_email_conversation(cabinet, monkeypatch):
    monkeypatch.setattr(agent, "cabinet_db", lambda: FakeDb())
    monkeypatch.setattr(agent, "call_n8n", lambda *a, **k: pytest.fail("n8n called"))
    with pytest.raises(ToolError, match="inconnue"):
        agent.ask_agent("mail-of-someone-else", "x")


@pytest.mark.communication
@pytest.mark.parametrize("answer", [{"ok": False, "code": "session_not_found", "message": "Session not found."},
                                    {"ok": True, "status": "suspended", "response": "?"}, {"ok": True, "status": "completed"}])
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
