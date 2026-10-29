"""The CGNC (Code Général de Normalisation Comptable) as Tarik looks it up: its pieces and its chart of accounts,
split from the PDF by split_cgnc.py into engine/knowledge/cgnc/, loaded into Postgres (schema orchestrator,
engine/sql/knowledge.sql) at each start when those files changed, searched by account code and by words.

The same for every cabinet: a public text, read only, never a cabinet's data."""

import hashlib
import json
from pathlib import Path

import psycopg

from engine.tools.core.queries import Q
from engine.tools.core.words import normalized

DATA = Path(__file__).resolve().parent / "cgnc"
FILES = ("pieces.jsonl", "accounts.jsonl")
LINKED = 20  # the other accounts told at most, the first the pieces name


def rows(name):
    """Return the rows of DATA's file ``name``, one JSON object per line."""
    return [json.loads(line) for line in (DATA / name).read_text().splitlines() if line]


def digest():
    """Return the sha256 of DATA's files, together: what was loaded, in knowledge_sources."""
    return hashlib.sha256(b"".join((DATA / name).read_bytes() for name in FILES)).hexdigest()


def load(conninfo):
    """Make the knowledge tables if missing and load the CGNC into them when its files changed since the last load:
    all of it replaced at once, in one transaction (a search meanwhile sees the old version, never half of it)."""
    with psycopg.connect(conninfo) as db:  # one transaction, committed on leaving
        db.execute(Q.knowledge_schema)
        db.execute(Q.knowledge_lock)
        current = digest()
        if (found := db.execute(Q.knowledge_digest, ("cgnc",)).fetchone()) and found[0] == current:
            return False
        db.execute(Q.cgnc_forget)
        with db.cursor() as cursor:
            cursor.executemany(
                Q.cgnc_piece_saved,
                [
                    {**piece, "place_words": normalized(piece["place"]), "body_words": normalized(piece["body"])}
                    for piece in rows("pieces.jsonl")
                ],
            )
            cursor.executemany(Q.cgnc_account_saved, rows("accounts.jsonl"))
        db.execute(Q.knowledge_loaded, ("cgnc", current))
        return True


def search(db, code="", words="", limit=5):
    """Return what the CGNC says about account ``code`` and/or ``words``, from connection ``db``:
    {account: [{code, label}] (the code asked and the accounts it is part of, the rubric first), pieces: [{place,
    pages, body}] (at most ``limit``, the most to the point first; any of the words counts, more of them close together
    rank higher), linked: [{code, label}] (the accounts of other
    rubrics those pieces name: an entry's other side, 3491 for 6125)}.

    A code the plan lacks (a dossier's own sub-account, 61251001) is looked up by the accounts it starts with."""
    code = "".join(character for character in str(code) if character.isdigit())
    within = [code[:size] for size in range(len(code), 1, -1)]  # 61251, 6125, 612, 61
    rubric = within[-1] if within else "-"
    asked = normalized(words).split()
    found = db.execute(
        Q.cgnc_found,
        {"codes": within, "any_words": " or ".join(asked), "all_words": " ".join(asked), "limit": limit},
    ).fetchall()
    account = db.execute(Q.cgnc_accounts_named, (within,)).fetchall()
    named = []
    for *_, codes, _ in found:
        # another rubric's: an entry's other side, not the account's own neighbours in the plan
        named += [one for one in codes if not one.startswith(rubric) and one not in named]
    linked = db.execute(Q.cgnc_accounts_named, (named[:LINKED],)).fetchall()
    return {
        "account": [{"code": one, "label": label} for one, label in account],
        "pieces": [
            {"place": place, "pages": f"{start}" if start == end else f"{start}-{end}", "body": body}
            for _, place, start, end, _, body in found
        ],
        "linked": [{"code": one, "label": label} for one, label in linked],
    }
