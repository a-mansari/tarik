"""The pages' live updates (engine/live.py): a change wakes only who shows it, at once; nothing else wakes them."""

import asyncio

import pytest

from engine import live


@pytest.mark.technical
def test_a_change_wakes_only_the_pages_showing_it():
    async def check():
        mine, other = [("cabinet", "cab_a")], [("session", "chat-b")]
        seen_mine, seen_other = live.versions(mine), live.versions(other)
        waiting = asyncio.gather(live.changed(mine, seen_mine, 2), live.changed(other, seen_other, 0.2))
        await asyncio.sleep(0.05)
        live.touched("run-1", "cab_a", "chat-a")
        woken, untouched = await waiting
        assert woken[("cabinet", "cab_a")] == seen_mine[("cabinet", "cab_a")] + 1
        assert untouched is None  # another conversation's page: still asleep, only its sign of life is sent

    asyncio.run(check())


@pytest.mark.technical
def test_a_change_made_before_waiting_is_not_missed():
    async def check():
        keys = [("run", "run-2")]
        seen = live.versions(keys)
        live.touched("run-2", "cab_a", "chat-a")
        assert await live.changed(keys, seen, 0.1) == {("run", "run-2"): seen[("run", "run-2")] + 1}

    asyncio.run(check())
