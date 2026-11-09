"""Tarik's memory (engine/memory.py): off without HINDSIGHT_URL, one bank per cabinet, never an error for a turn."""

import asyncio

import httpx
import pytest

from engine import memory


@pytest.fixture
def sent(monkeypatch):
    """Turn the memory on and record each request it sends (bank, path, body); the answer is ``answers`` (a dict)."""
    calls, answers = [], {"results": [{"text": "Karim : TVA trimestrielle"}]}

    async def post(bank, path, body, wait):
        calls.append((bank, path, body))
        return answers

    monkeypatch.setattr(memory, "URL", "http://hindsight:8888")
    monkeypatch.setattr(memory, "post", post)
    return calls


def test_off_without_url_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(memory, "URL", "")
    assert asyncio.run(memory.recalled("experio_cabinet_8", "TVA ?")) == ""
    assert asyncio.run(memory.retained("experio_cabinet_8", "s1", "a")) is None


def test_recall_reads_the_cabinets_own_bank(sent):
    assert asyncio.run(memory.recalled("experio_cabinet_8", "TVA ?")) == "- Karim : TVA trimestrielle"
    assert [call[0] for call in sent] == ["experio_cabinet_8"]


def test_recall_without_a_question_asks_nothing(sent):
    assert asyncio.run(memory.recalled("experio_cabinet_8", "  ")) == ""
    assert sent == []


def test_retain_keeps_only_the_client_s_words_in_the_cabinets_own_bank(sent):
    asyncio.run(memory.retained("experio_cabinet_3", "s1", "Bonjour"))
    bank, path, body = sent[0]
    assert (bank, path, body["async"]) == ("experio_cabinet_3", "memories", True)
    assert body["items"][0]["content"] == "Bonjour"


def test_long_texts_are_cut_and_empty_ones_not_kept(sent):
    asyncio.run(memory.retained("c", "s1", "x" * 10_000))
    asyncio.run(memory.retained("c", "s1", "  "))
    asyncio.run(memory.recalled("c", "y" * 10_000))
    assert len(sent) == 2
    assert len(sent[0][2]["items"][0]["content"]) == memory.KEPT_CHARS
    assert len(sent[1][2]["query"]) == memory.QUERY_CHARS


def test_retains_going_on_are_capped(monkeypatch):
    async def go():
        monkeypatch.setattr(memory, "BACKGROUND", {object()})
        monkeypatch.setattr(memory, "BACKGROUND_MAX", 1)
        monkeypatch.setattr(memory, "log", lambda line: None)
        memory.retain_later("c", "s", "bonjour")  # dropped: no task made, nothing to await
        assert len(memory.BACKGROUND) == 1

    asyncio.run(go())


@pytest.mark.parametrize("status, logged, gives", [(404, False, ""), (500, True, None)])
def test_recall_failing_gives_nothing(monkeypatch, status, logged, gives):
    lines = []

    async def post(*_):
        raise httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://h"), response=httpx.Response(status))

    monkeypatch.setattr(memory, "URL", "http://hindsight:8888")
    monkeypatch.setattr(memory, "post", post)
    monkeypatch.setattr(memory, "log", lines.append)
    assert asyncio.run(memory.recalled("experio_cabinet_8", "TVA ?")) == gives
    assert bool(lines) is logged  # a bank not made yet (404) is no failure


def test_retain_failing_is_logged_not_raised(monkeypatch):
    lines = []

    async def post(*_):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(memory, "URL", "http://hindsight:8888")
    monkeypatch.setattr(memory, "post", post)
    monkeypatch.setattr(memory, "log", lines.append)
    asyncio.run(memory.retained("experio_cabinet_8", "s1", "a"))
    assert len(lines) == 1


class Request:
    """A model request as the middleware sees it: the state, the system message, override()."""

    def __init__(self, messages, system="Tu es Tarik."):
        from langchain_core.messages import SystemMessage

        self.state, self.system_message = {"messages": messages}, SystemMessage(system)

    def override(self, **changes):
        found = Request(self.state["messages"])
        found.system_message = changes["system_message"]
        return found


def system_seen(request, monkeypatch):
    from engine import agent

    monkeypatch.setattr(agent, "get_config", lambda: {"configurable": {"cabinet": "experio_cabinet_8"}})
    seen = []

    async def handler(asked):
        seen.append(asked.system_message.text)

    asyncio.run(agent.remembered.awrap_model_call(request, handler))
    return seen[0]


@pytest.mark.security
def test_memories_reach_the_model_as_untrusted_data_once_per_message(sent, monkeypatch):
    from langchain_core.messages import HumanMessage

    from engine import agent

    agent.RECALLED.clear()
    asked = Request([HumanMessage("Ma TVA ?")])
    seen = system_seen(asked, monkeypatch)
    assert 'label="memoire"' in seen and "TVA trimestrielle" in seen
    assert 'source="Experio"' not in seen  # what clients said is not Experio's data
    system_seen(asked, monkeypatch)  # a second model call of the same turn
    assert len(sent) == 1


def test_a_failed_recall_is_asked_again_not_kept_as_nothing(sent, monkeypatch):
    from langchain_core.messages import HumanMessage

    from engine import agent

    agent.RECALLED.clear()
    asked = Request([HumanMessage("Ma TVA ?")])
    monkeypatch.setattr(memory, "recalled", lambda *args: asyncio.sleep(0, None))
    assert system_seen(asked, monkeypatch) == "Tu es Tarik."
    monkeypatch.undo()
    assert agent.RECALLED == {}


def test_a_routine_s_turn_or_no_memory_leaves_the_prompt_as_it_is(sent, monkeypatch):
    from langchain_core.messages import HumanMessage

    from engine import agent

    agent.RECALLED.clear()
    assert system_seen(Request([HumanMessage("[Routine]", name=agent.ROUTINE_NAME)]), monkeypatch) == "Tu es Tarik."
    monkeypatch.setattr(memory, "URL", "")
    assert system_seen(Request([HumanMessage("Ma TVA ?")]), monkeypatch) == "Tu es Tarik." and sent == []
