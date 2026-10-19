"""Every tool uses the model the cabinet's agent runs on in n8n, asked of n8n and kept a few minutes."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core import cabinets, model
from tarik.core.cabinets import using

CABINET = "tarik_test_a"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(model, "_models", {})
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({CABINET}))
    monkeypatch.setenv("TARIK_TEST_A__AGENT_ID", "agent-1")


def n8n(monkeypatch, answers):
    asked = []

    def call_n8n(tool, **arguments):
        asked.append((tool, arguments))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"config": {"model": answer}}

    monkeypatch.setattr("tarik.emails.agent.call_n8n", call_n8n)
    return asked


def test_the_agents_model_without_its_provider(monkeypatch):
    asked = n8n(monkeypatch, ["minimax/MiniMax-M3"])
    with using(CABINET):
        assert model.model_name() == "MiniMax-M3"
        assert model.model_name() == "MiniMax-M3"  # kept: n8n is asked once
    assert asked == [("get_agent", {"agentId": "agent-1"})]


def test_a_model_changed_in_n8n_is_used_after_a_while(monkeypatch):
    n8n(monkeypatch, ["minimax/MiniMax-M3", "minimax/MiniMax-M4"])
    with using(CABINET):
        assert model.model_name() == "MiniMax-M3"
        monkeypatch.setattr(model.time, "monotonic", lambda: 10**9)
        assert model.model_name() == "MiniMax-M4"


def test_the_last_model_when_n8n_cannot_answer(monkeypatch):
    n8n(monkeypatch, ["minimax/MiniMax-M3", RuntimeError("down")])
    with using(CABINET):
        assert model.model_name() == "MiniMax-M3"
        monkeypatch.setattr(model.time, "monotonic", lambda: 10**9)
        assert model.model_name() == "MiniMax-M3"


def test_no_model_at_all_is_a_tool_error(monkeypatch):
    n8n(monkeypatch, [RuntimeError("down")])
    with using(CABINET), pytest.raises(ToolError):
        model.model_name()
