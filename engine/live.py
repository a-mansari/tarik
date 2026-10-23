"""What changed, for the pages to follow Tarik near real time, as n8n's do, without asking the database again and
again: each run created, each step recorded and each run ended counts one change for its run, its cabinet and its
conversation (touched). A browser holds one event stream for all its pages (server.py: /ui/live, web/src/live.js)
that sleeps until a change one of them shows happens; that page then fetches what it needs once. A page nobody looks
at costs no query, however many are open.

shortcut: one server process (the changes live in its memory); several would need Postgres LISTEN/NOTIFY."""

import asyncio

VERSIONS = {}  # ("run" | "cabinet" | "session", its id) -> how many changes so far (a few small entries per run)
_tick = asyncio.Event()  # set, then replaced, at each change: whoever waits on it wakes and compares versions


def touched(run_id, cabinet, session):
    """One change of run ``run_id``, in ``cabinet`` and conversation ``session``: every page showing one of them wakes.
    Called from the server's event loop only."""
    global _tick
    for key in (("run", run_id), ("cabinet", cabinet), ("session", session)):
        VERSIONS[key] = VERSIONS.get(key, 0) + 1
    tick, _tick = _tick, asyncio.Event()
    tick.set()


def versions(keys):
    return {key: VERSIONS.get(key, 0) for key in keys}


async def changed(keys, seen, seconds=25.0):
    """The versions of ``keys`` once one of them differs from ``seen``, or None after ``seconds`` without a change
    (the caller then shows it is still there, which also finds a page that left)."""
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
