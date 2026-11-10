"""The audit trail: one JSON line per event on the standard output, for the operator to collect. It names who (a short
fingerprint of the login, never the key), where (the cabinet), what (the tool, the dossier, the model) and how it ended;
never a value Experio returned, a filter's value, a login or a key."""

import hashlib
import json
import sys
import time


def fingerprint(*parts):
    """Return a short, one-way fingerprint of ``parts`` (a login): enough to tell users apart in the trail."""
    return hashlib.sha256("|".join(parts).lower().encode()).hexdigest()[:10]


def event(kind, **fields):
    """Write one audit line. ``fields`` hold no secret and no data: the callers pass names and counts only."""
    print(
        json.dumps({"at": round(time.time(), 3), "event": kind, **fields}, ensure_ascii=False),
        file=sys.stdout,
        flush=True,
    )
