"""Change counters, so the pages follow Tarik in near real time without polling the database.

Each run created, step recorded, run ended (and prompt saved) counts one change for its run, its cabinet and its
conversation (touched). A browser holds one event stream for all its pages (server/server.py: /ui/live,
server/web/src/live.js) that sleeps until something one of them shows changes; that page then fetches what it needs
once. An idle page costs no query, however many are open.

shortcut: one server process (the changes live in its memory); several would need Postgres LISTEN/NOTIFY."""

import asyncio

VERSIONS = {}  # ("run" | "cabinet" | "session", its id) -> how many changes so far (a few small entries per run)
_tick = asyncio.Event()  # set, then replaced, at each change: whoever waits on it wakes and compares versions


def touched(run_id, cabinet, session):
    """Count one change for run ``run_id``, ``cabinet`` and conversation ``session`` and wake every waiting page.
    Call from the server's event loop only."""
    global _tick
    for key in (("run", run_id), ("cabinet", cabinet), ("session", session)):
        VERSIONS[key] = VERSIONS.get(key, 0) + 1
    tick, _tick = _tick, asyncio.Event()
    tick.set()


def versions(keys):
    """Return {key: change count} for ``keys``, 0 for a key never changed."""
    return {key: VERSIONS.get(key, 0) for key in keys}


async def changed(keys, seen, seconds=25.0):
    """Wait until the versions of ``keys`` differ from ``seen`` and return them; None after ``seconds`` without a
    change (the caller then sends a keep-alive, which also detects a page that left)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while (now := versions(keys)) == seen:
        left = deadline - loop.time()
        if left <= 0:
            return None
        try:
            await asyncio.wait_for(_tick.wait(), left)
        except TimeoutError:
            return None
    return now
