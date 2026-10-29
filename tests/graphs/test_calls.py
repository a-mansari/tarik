"""engine.calls: one tool call in its cabinet, its refusal or failure, and what a step records."""

import asyncio

import pytest

from engine import calls
from engine.tools.core import cabinets
from engine.tools.core.app import CRASHED, ToolError


def refuse(dossier_id: int):
    raise ToolError("Dossier introuvable.")


def crash(dossier_id: int):
    raise ValueError("secret detail")


@pytest.mark.functionality
def test_a_step_whose_run_is_stopped_ends_its_tool_and_is_recorded_before_it_stops(monkeypatch):
    """What it did in Experio is never left unrecorded, and its slot stays taken until its thread ends."""
    import threading

    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"c1"}))
    monkeypatch.setattr(calls, "SLOTS", {})
    started, done, recorded = threading.Event(), [], []

    def slow(dossier_id: int):
        started.set()
        threading.Event().wait(0.3)
        done.append(dossier_id)
        return {"status": "ok", "message": "fait"}

    monkeypatch.setitem(calls.TOOLS, "fake_slow", slow)
    monkeypatch.setitem(calls.TOOLS, "record_step", lambda **given: recorded.append(given["result"]))

    async def check():
        task = asyncio.create_task(calls.step("c1", "r1", "s1", "fake_slow", dossier_id=7))
        await asyncio.to_thread(started.wait)
        task.cancel()
        assert calls.SLOTS["c1"]._value == calls.PER_CABINET - 1  # its thread still works
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done == [7] and recorded == [{"status": "ok", "message": "fait"}]
        assert calls.SLOTS["c1"]._value == calls.PER_CABINET

    asyncio.run(check())


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"c1"}))
    for name, function in {
        "fake_cabinet": lambda: cabinets.current(),
        "fake_refuse": refuse,
        "fake_crash": crash,
    }.items():
        monkeypatch.setitem(calls.TOOLS, name, function)


@pytest.mark.security
def test_a_tool_runs_in_the_cabinet_named(tools):
    assert asyncio.run(calls.call("c1", "fake_cabinet")) == "c1"


@pytest.mark.security
@pytest.mark.parametrize(
    "tool, arguments, said",
    [
        ("no_such_tool", {}, "outil inconnu : no_such_tool"),
        ("fake_refuse", {"wrong": 1}, calls.INVALID),
        ("fake_refuse", {"dossier_id": 1}, "Dossier introuvable."),
        ("fake_crash", {"dossier_id": 1}, CRASHED),
    ],
)
def test_a_refusal_is_said_in_the_tools_words_and_anything_else_never_shown(tools, tool, arguments, said):
    with pytest.raises(calls.ToolFailure) as failure:
        asyncio.run(calls.call("c1", tool, **arguments))
    assert str(failure.value) == said


@pytest.mark.security
def test_a_cabinet_not_served_is_never_run_in(tools):
    with pytest.raises(calls.ToolFailure):
        asyncio.run(calls.call("other", "fake_cabinet"))


@pytest.fixture
def recorded(monkeypatch):
    """calls.call faked: ``answers`` per tool (a ToolFailure raised), the record_step calls kept."""
    records, answers = [], {}

    async def call(cabinet, tool, /, **arguments):
        if tool == "record_step":
            records.append((arguments["origin"], arguments["result"]))
            return {}
        if isinstance(answers.get(tool), Exception):
            raise answers[tool]
        return answers.get(tool, {"status": "ok"})

    monkeypatch.setattr(calls, "call", call)
    return records, answers


@pytest.mark.technical
def test_a_failed_step_becomes_an_error_outcome_with_the_ids_it_was_given(recorded):
    records, answers = recorded
    answers["post_invoice"] = calls.ToolFailure("Refuse.")
    result = asyncio.run(calls.step("c1", "r1", "s1", "post_invoice", invoice_id=4, other="x"))
    assert result == {"status": "error", "message": "Refuse.", "invoice_id": 4}
    assert records == [("post_invoice", result)]


@pytest.mark.communication
@pytest.mark.parametrize(
    "answer, kept",
    [({"status": "ok"}, False), ({"status": "ok", "tell": True}, True), ({"status": "stop"}, True)],
)
def test_a_plumbing_step_is_recorded_only_when_not_ok_or_told(recorded, answer, kept):
    records, answers = recorded
    answers["files_to_process"] = answer
    asyncio.run(calls.step("c1", "r1", "s1", "files_to_process"))
    assert bool(records) is kept


@pytest.mark.communication
def test_an_answer_is_recorded_under_the_step_it_answers(recorded):
    records, answers = recorded
    answers["answer_pending"] = {"status": "ok", "step": "settle_tva"}
    asyncio.run(calls.step("c1", "r1", "s1", "answer_pending"))
    answers["answer_pending"] = {"status": "ok"}
    asyncio.run(calls.step("c1", "r1", "s1", "answer_pending"))
    assert [origin for origin, _ in records] == ["settle_tva", "answer_pending"]
