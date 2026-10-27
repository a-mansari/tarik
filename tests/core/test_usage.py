"""Model calls' tokens and cost: kept under the run they were made in (a tool's thread too) and the agent's prompt,
then totalled per run (its nested runs included), per conversation and per agent call. Against this run's own
orchestrator schema and cabinet in the test database."""

import asyncio

import pytest

from engine import calls, prompts, runs
from engine.tools.core import usage
from engine.tools.core.queries import Q
from tests.api.test_team_accounts import against_the_test_store


@pytest.mark.functionality
def test_a_run_s_model_calls_add_up_with_its_nested_runs_conversation_and_prompt(db_cabinet, monkeypatch):
    async def check():
        parent = await runs.created("agent", {}, db_cabinet, "chat-a")
        child = await runs.created("traitement", {}, db_cabinet, "chat-a", parent=parent)
        other = await runs.created("agent", {}, db_cabinet, "chat-b")
        running = usage.RUNNING.set(child)
        prompt = usage.PROMPT_CALL.set(7)
        try:  # a tool's model call, in its worker thread: the run and the prompt reach it
            await calls.within(db_cabinet, usage.counted, "MiniMax-M3", 1000, 500, 200)
        finally:
            usage.PROMPT_CALL.reset(prompt)
            usage.RUNNING.reset(running)
        usage.RUNNING.set(other)
        await calls.within(db_cabinet, usage.counted, "claude-x", 10, 0, 5, 0.25)

        by_run = await prompts.spent(db_cabinet, Q.usage_of_runs, [parent, child, other])
        assert by_run[parent] == by_run[child] and by_run[parent][:3] == (1000, 500, 200)
        assert float(by_run[parent][3]) == pytest.approx(0.00057) and by_run[other][3:] == (0.25, True)
        by_session = await prompts.spent(db_cabinet, Q.usage_of_sessions, ["chat-a", "chat-b", "chat-c"])
        assert set(by_session) == {"chat-a", "chat-b"} and by_session["chat-a"][:3] == (1000, 500, 200)
        assert (await prompts.spent(db_cabinet, Q.usage_of_calls, [7, 8])).keys() == {7}

    against_the_test_store(check, monkeypatch)


@pytest.mark.communication
def test_a_total_missing_a_model_s_price_says_so():
    from server.api.runs import usage_shown

    assert usage_shown(1_000, 500, 200, 0.5, False)["cost"] == "0,50 $ + ?"
    assert usage_shown(10, 0, 5, None, False) == {
        "tokens": "15",
        "detail": "10 en entrée (dont 0 en cache), 5 en sortie",
        "cost": "prix inconnu",
    }


@pytest.mark.functionality
def test_an_agent_model_call_s_tokens_are_kept_under_its_saved_prompt(monkeypatch):
    from types import SimpleNamespace

    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.messages import AIMessage

    from engine import agent

    config = {"configurable": {"cabinet": "cabinet_a", "session_id": "chat-a"}}
    monkeypatch.setattr(prompts, "get_config", lambda: config)
    monkeypatch.setattr(agent, "get_config", lambda: config)

    async def save(*args):
        return 7

    monkeypatch.setattr(prompts, "save", save)
    kept = []
    monkeypatch.setattr(agent, "counted_in", lambda *call: kept.append((*call, usage.PROMPT_CALL.get())))
    used = {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050, "input_token_details": {"cache_read": 400}}

    async def model(request):
        return ModelResponse(result=[AIMessage("ok", usage_metadata=used)])

    async def made(request):  # the order make_agent gives them: logged around usage_counted
        return await agent.usage_counted.awrap_model_call(request, model)

    request = SimpleNamespace(system_message=None, tools=[], messages=[])
    asyncio.run(prompts.logged.awrap_model_call(request, made))
    assert kept == [("cabinet_a", agent.AGENT["model"], 600, 400, 50, 7)] and usage.PROMPT_CALL.get() is None
