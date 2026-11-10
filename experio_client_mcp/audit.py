"""The audit trail: one JSON line per event on the standard output, for the operator to collect. It names who (a short
keyed fingerprint of the login, never the key), where (the cabinet), what (the tool, the dossier, a known model) and how
it ended; never a value Experio returned, a filter's value, a login or a key. Lines are queued and written by one
thread, so a stalled log pipe never stops the server; a full queue drops the line."""

import contextlib
import hashlib
import hmac
import json
import queue
import sys
import threading
import time

from experio_client_mcp import config

_lines = queue.Queue(maxsize=10_000)


def _write():
    while True:
        print(_lines.get(), file=sys.stdout, flush=True)
        _lines.task_done()


threading.Thread(target=_write, daemon=True).start()  # shortcut: lines still queued are lost on a hard stop


def drain():
    """Wait until every queued line is written (tests, shutdown)."""
    _lines.join()


def fingerprint(*parts):
    """Return a short fingerprint of ``parts`` (a login), keyed by the operator's secret: enough to tell users apart in
    the trail, no way to confirm a guessed login from the logs alone."""
    return hmac.new(config.AUDIT_SECRET.encode(), "|".join(parts).lower().encode(), hashlib.sha256).hexdigest()[:10]


def event(kind, **fields):
    """Queue one audit line. ``fields`` hold no secret and no data: the callers pass names and counts only."""
    with contextlib.suppress(queue.Full):
        _lines.put_nowait(json.dumps({"at": round(time.time(), 3), "event": kind, **fields}, ensure_ascii=False))
