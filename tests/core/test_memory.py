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


def fake_hindsight(monkeypatch, banks, operations):
    """Hindsight's banks and each bank's failed operations (or an exception to raise): what failures() reads."""
    asked = []

    async def hindsight(client, method, path, **kwargs):
        asked.append((method, path, kwargs.get("params")))
        if path == "/banks":
            return {"banks": [{"bank_id": bank} for bank in banks]}
        found = operations[path.split("/")[2]]
        if isinstance(found, Exception):
            raise found
        return {"operations": found}

    monkeypatch.setattr(memory, "URL", "http://hindsight:8888")
    monkeypatch.setattr(memory, "hindsight", hindsight)
    return asked


def operation(days_ago, error="boom", **more):
    at = (memory.datetime.now(memory.UTC) - memory.timedelta(days=days_ago)).isoformat()
    return {
        "id": f"op-{days_ago}",
        "created_at": at,
        "updated_at": at,
        "operation_type": "retain",
        "error_message": error,
        **more,
    }


def test_failures_of_the_last_days_from_every_bank_newest_first_with_the_error_cut(monkeypatch):
    asked = fake_hindsight(
        monkeypatch,
        ["experio_cabinet_3", "experio_cabinet_8"],
        {
            "experio_cabinet_3": [operation(2), operation(30)],  # the second: fixed long since, forgotten
            "experio_cabinet_8": [operation(1, "x" * 500)],
        },
    )
    found = asyncio.run(memory.failures())
    assert [(one["cabinet"], one["id"]) for one in found] == [
        ("experio_cabinet_8", "op-1"),
        ("experio_cabinet_3", "op-2"),
    ]
    assert len(found[0]["error"]) == 300 and found[0]["kind"] == "retain"
    assert asked[1][2] == {"status": "failed", "exclude_parents": "true", "limit": memory.FAILED_PER_BANK}


def test_a_row_without_time_or_kind_is_listed_not_a_crash_and_a_bank_that_fails_is_left_out(monkeypatch):
    lines = []
    fake_hindsight(
        monkeypatch,
        ["a", "b"],
        {"a": [{"error_message": None}, operation(1)], "b": httpx.ConnectError("down")},
    )
    monkeypatch.setattr(memory, "log", lines.append)
    found = asyncio.run(memory.failures())
    assert [one["id"] for one in found] == ["op-1", ""] and found[1]["at"] is None and found[1]["kind"] == ""
    assert len(lines) == 1 and lines[0].startswith("b memory failures")


def test_a_bank_name_cannot_change_the_path(monkeypatch):
    asked = []

    async def hindsight(client, method, path, **kwargs):
        asked.append(path)
        return {"banks": [{"bank_id": "x/../y?z"}]} if path == "/banks" else {"operations": []}

    monkeypatch.setattr(memory, "URL", "http://hindsight:8888")
    monkeypatch.setattr(memory, "hindsight", hindsight)
    asyncio.run(memory.failures())
    assert asked[1] == "/banks/x%2F..%2Fy%3Fz/operations"


def test_no_failure_when_off_and_none_when_hindsight_is_down_or_too_slow(monkeypatch):
    monkeypatch.setattr(memory, "URL", "")
    assert asyncio.run(memory.failures()) == []
    lines = []

    async def down(*_, **__):
        raise httpx.ConnectError("down")

    async def slow(*_, **__):
        await asyncio.sleep(5)

    monkeypatch.setattr(memory, "URL", "http://hindsight:8888")
    monkeypatch.setattr(memory, "log", lines.append)
    monkeypatch.setattr(memory, "FAILED_WAIT", 0.05)
    for fake in (down, slow):
        monkeypatch.setattr(memory, "hindsight", fake)
        assert asyncio.run(memory.failures()) is None
    assert len(lines) == 2
