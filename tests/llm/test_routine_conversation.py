"""A conversation a routine began, as Tarik's memory keeps it (the routine's user-side line, Tarik's report, the
client's reply), sent to each model a cabinet may use (engine/agent.py model: MiniMax through its Anthropic-style
API, Claude through the Claude Code CLI): each one answers it. Real calls, billed and slow: python -m pytest -m live
tests/llm/test_routine_conversation.py."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from engine import agent
from engine.tools.core import model
from tests.llm.conftest import CABINET


@pytest.mark.live
@pytest.mark.parametrize("chosen", [model.MINIMAX, model.CLAUDE])
def test_each_model_answers_a_conversation_a_routine_began(monkeypatch, chosen):
    monkeypatch.setattr(agent, "llm", lambda: model.llm_of(chosen))
    history = [
        SystemMessage("Tu es Tarik, l'assistant comptable du cabinet. Réponds en une phrase."),
        HumanMessage(agent.BEGAN.format("Rappel Manager"), name=agent.ROUTINE_NAME),
        AIMessage(agent.untrusted("Comment dois-je avancer aujourd'hui ?", "rapport_envoye")),
        HumanMessage("Commence par les factures du dossier ALPHA."),
    ]
    answer = agent.model(CABINET).invoke(history)
    assert answer.text.strip(), f"{chosen}: no answer"
