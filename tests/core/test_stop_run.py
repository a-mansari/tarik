"""A run stopped by hand: it ends in error saying who stopped it, with the run it was started from and every run
nested in it, whether it was running or still waiting for its slot. Against this run's own orchestrator schema in
the test database."""

import asyncio

import pytest

from engine import runs
from engine.tools.core.queries import Q
from tests.api.test_team_accounts import against_the_test_store


class Slow:
    """A graph that goes on until stopped, first starting a nested run of itself when ``nested``."""

    def __init__(self, nested):
        self.nested = nested

    async def astream(self, given, config, **options):
        if self.nested:
            await runs.nested("dedans", {}, "c", "s")
        await asyncio.sleep(60)
        yield ()


@pytest.fixture(autouse=True)
def slow_graphs(monkeypatch):
    """Every graph replaced by Slow, nested for "dehors"."""
    monkeypatch.setattr(runs, "graph_of", lambda name, configurable: Slow(nested=name == "dehors"))


async def state(run_id):
    async with runs.pool.connection() as db:
        return await (await db.execute(Q.run_state, (run_id,))).fetchone()


async def until(found):
    """Wait (2 s at most) until the async ``found()`` gives something, and return it."""
    for _ in range(200):
        if value := await found():
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def child_of(run_id):
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.run_children, (run_id,))).fetchall()
    return rows and rows[0][0]


@pytest.mark.functionality
def test_a_running_run_stopped_ends_saying_who_stopped_it(monkeypatch):
    freed = []

    async def within(cabinet, function, *args):
        freed.append((cabinet, function.__name__, *args))

    monkeypatch.setattr(runs, "within", within)

    async def check():
        run_id, task = await runs.start("seul", {"input": {}, "cabinet": "c"})
        await until(lambda: state_is(run_id, "running"))
        stopped = await asyncio.gather(runs.stop(run_id, "admin@cabinet.ma"), runs.stop(run_id, "autre@cabinet.ma"))
        assert stopped == [run_id, run_id]  # the second while it stops: not cancelled twice
        with pytest.raises(asyncio.CancelledError):
            await task
        status, why = await state(run_id)  # the words of whichever came first
        assert status == "error" and why in ("Arrêté par admin@cabinet.ma", "Arrêté par autre@cabinet.ma")
        assert freed == [("c", "released", run_id)]  # the invoices it claimed, free for another run
        assert run_id not in runs.TASKS and run_id not in runs.STOPPED
        assert await runs.stop(run_id, "admin@cabinet.ma") is None  # no longer going on
        assert await runs.stop("00000000-0000-0000-0000-000000000000", "admin@cabinet.ma") is None  # no such run
        assert not runs.is_id("abc") and runs.is_id(run_id)

    against_the_test_store(check, monkeypatch)


@pytest.mark.functionality
def test_stopping_a_nested_run_stops_its_whole_tree(monkeypatch):
    async def check():
        root, task = await runs.start("dehors", {"input": {}, "cabinet": "c"})
        nested = await until(lambda: child_of(root))
        await until(lambda: state_is(nested, "running"))
        assert await runs.stop(nested, "admin@cabinet.ma") == root
        with pytest.raises(asyncio.CancelledError):
            await task
        for run_id in (root, nested):
            assert await state(run_id) == ("error", "Arrêté par admin@cabinet.ma")

    against_the_test_store(check, monkeypatch)


@pytest.mark.functionality
def test_a_run_waiting_for_its_slot_stopped_ends_too(monkeypatch):
    async def check():
        busy = asyncio.Semaphore(0)  # no slot free
        run_id, task = await runs.start("seul", {"input": {}, "cabinet": "c"}, slots=busy)
        await asyncio.sleep(0.05)
        assert await state(run_id) == ("queued", None)
        await runs.stop(run_id, "admin@cabinet.ma")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await state(run_id) == ("error", "Arrêté par admin@cabinet.ma")

    against_the_test_store(check, monkeypatch)


async def state_is(run_id, status):
    found = await state(run_id)
    return found and found[0] == status


@pytest.mark.functionality
def test_a_stopped_emails_run_sets_its_threads_aside_saying_why(monkeypatch):
    from engine import emails

    set_aside = []

    async def nested(*given, **options):
        await asyncio.sleep(60)

    async def call(cabinet, tool, **arguments):
        set_aside.append((tool, arguments["thread_id"], arguments["sent"], arguments["reason"]))

    monkeypatch.setattr(runs, "nested", nested)
    monkeypatch.setattr(emails, "call", call)
    threads = [{"thread_id": "<m1@x>", "session_id": "s1", "message_ids": ["<m1@x>"]}]

    async def check():
        runs.ROOT.set("r1")
        runs.STOPPED["r1"] = "Arrêté par admin@cabinet.ma"
        try:
            task = asyncio.create_task(emails.each_thread({"cabinet": "c", "threads": threads}))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            runs.STOPPED.pop("r1")
        assert set_aside == [("finish_thread", "<m1@x>", False, "Arrêté par admin@cabinet.ma")]

    asyncio.run(check())


@pytest.mark.functionality
def test_a_stopped_routine_is_reported_never_run_again(monkeypatch):
    from engine import routines as clock

    reported = []

    async def start(name, payload, **options):
        async def forever():
            await asyncio.sleep(60)

        task = asyncio.create_task(forever())
        asyncio.get_running_loop().call_later(0.01, task.cancel)  # stopped by hand (runs.stop)
        return "r1", task

    async def within(cabinet, function, *args):
        reported.append(args)

    monkeypatch.setattr(clock.runs, "start", start)
    monkeypatch.setattr(clock, "within", within)
    monkeypatch.setattr(clock, "given", lambda cabinet, row: {})
    row = (5, None, None, None, None, None, None, "2026-10-29 08:00")
    assert asyncio.run(clock.ran("c", row)) == clock.STOPPED
    assert reported == [(5, "2026-10-29 08:00", clock.STOPPED)]


@pytest.mark.communication
def test_a_tool_call_its_stopped_run_left_unanswered_is_answered_for_the_model():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from engine import agent

    asked = AIMessage(
        "", tool_calls=[{"id": "t1", "name": "Experio_x", "args": {}}, {"id": "t2", "name": "y", "args": {}}]
    )
    done = ToolMessage("fait", tool_call_id="t2", name="y")
    found = agent.answered([HumanMessage("Bonjour"), asked, done, HumanMessage("Et alors ?")])
    assert [type(message).__name__ for message in found] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    assert found[2].tool_call_id == "t1" and found[2].content == agent.UNANSWERED and found[3] is done
