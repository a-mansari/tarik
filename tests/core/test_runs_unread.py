"""The runs an account has not opened: only runs started on their own, after the account was made, in the cabinet
asked for; opening one, or marking all read, takes it off the count. Against this run's own orchestrator schema."""

import uuid

from engine import runs
from tests.api.test_team_accounts import against_the_test_store

ME, OTHER = "me@cabinet.ma", "other@cabinet.ma"


async def run_in(db, cabinet, when="now()", parent=None):
    run_id = str(uuid.uuid4())
    await db.execute(
        f"insert into runs (id, graph, cabinet, session_id, status, input, started_at, parent_id)"
        f" values (%s, 'agent', %s, 's', 'done', '{{}}', {when}, %s)",
        (run_id, cabinet, parent),
    )
    return run_id


def test_an_account_counts_the_runs_it_has_not_opened(monkeypatch):
    async def check():
        async with runs.pool.connection() as db:
            for email in (ME, OTHER):
                await db.execute("insert into users (email, name, password) values (%s, 'x', null)", (email,))
            await run_in(db, "cab_a", "now() - interval '1 day'")  # before the accounts: never unread
            first, second = await run_in(db, "cab_a"), await run_in(db, "cab_a")
            child = await run_in(db, "cab_a", parent=first)  # nested: counted with its parent
            await run_in(db, "cab_b")
        assert await runs.unread("cab_a", ME) == 2 and await runs.unread("cab_b", ME) == 1
        await runs.read(child, ME)  # opening a nested run reads the run it belongs to
        assert await runs.unread("cab_a", ME) == 1 and await runs.unread("cab_a", OTHER) == 2
        await runs.read(first, "script")  # no account: nothing kept, no error
        await runs.read_all("cab_a", ME)
        assert await runs.unread("cab_a", ME) == 0 and await runs.unread("cab_b", ME) == 1
        assert second  # opened by read_all

    against_the_test_store(check, monkeypatch)
