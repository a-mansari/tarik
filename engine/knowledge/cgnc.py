"""The CGNC (Code Général de Normalisation Comptable) as Tarik looks it up: its pieces and its chart of accounts,
split from the PDF by split_cgnc.py into engine/knowledge/cgnc/, loaded into Postgres (schema knowledge,
engine/sql/knowledge.sql) at each start when they changed, searched by account code and by words.

The same for every cabinet: a public text, never a cabinet's data."""

import hashlib
import json
from pathlib import Path

import psycopg

from engine.tools.core.app import ToolError, tool
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized

DATA = Path(__file__).resolve().parent / "cgnc"
FILES = ("pieces.jsonl", "accounts.jsonl")
LIMIT = 10  # the pieces told at most in one search
LINKED = 20  # the other accounts told at most, the first the pieces name


def rows(name):
    """Return the rows of DATA's file ``name``, one JSON object per line."""
    return [json.loads(line) for line in (DATA / name).read_text().splitlines() if line]


def digest():
    """Return the sha256 of DATA's files and of the statements that load them, together: the version loaded, kept
    in knowledge.sources (a change to how the rows are made reloads them too)."""
    loading = (Q.knowledge_schema, Q.cgnc_piece_saved, Q.cgnc_account_saved)
    return hashlib.sha256(
        b"".join((DATA / name).read_bytes() for name in FILES) + "".join(loading).encode()
    ).hexdigest()


def load(conninfo):
    """Make the knowledge tables if missing and load the CGNC into them when its version changed since the last
    load: all of it replaced at once, in one transaction (a search meanwhile reads the old version, never half of
    it). Returns True when it loaded."""
    with psycopg.connect(conninfo) as db:  # one transaction, committed on leaving
        db.execute(Q.knowledge_lock)
        db.execute(Q.knowledge_schema)
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
    pages, body}] (at most ``limit``, LIMIT at most, the most to the point first: holding all the words, then
    naming the code, then the closest), linked: [{code, label}] (the accounts of other rubrics those pieces name, the
    first named first: an entry's other side, 3491 for 6125)}.

    A code the plan lacks (a dossier's own sub-account, 61251001) is looked up by the accounts it starts with."""
    code = "".join(character for character in str(code) if character.isdigit())
    within = [code[:size] for size in range(len(code), 1, -1)]  # 61251, 6125, 612, 61
    rubric = within[-1] if within else "-"
    asked = normalized(words).split()
    found = db.execute(
        Q.cgnc_found,
        {
            "codes": within,
            "any_words": " or ".join(asked),
            "all_words": " ".join(asked),
            "limit": max(1, min(int(limit or LIMIT), LIMIT)),
        },
    ).fetchall()
    # another rubric's: an entry's other side, not the account's own neighbours in the plan
    named = list(dict.fromkeys(one for *_, codes, _ in found for one in codes if not one.startswith(rubric)))
    labels = dict(db.execute(Q.cgnc_accounts_named, (within + named,)).fetchall())
    return {
        "account": [{"code": one, "label": labels[one]} for one in reversed(within) if one in labels],
        "pieces": [
            {"place": place, "pages": f"{start}" if start == end else f"{start}-{end}", "body": body}
            for place, start, end, _, body in found
        ],
        "linked": [{"code": one, "label": labels[one]} for one in named if one in labels][:LINKED],
    }


@tool
def consult_cgnc(code: str = "", words: str = "") -> str:
    """What the CGNC says about an account and/or words, as text to answer from: the account and the accounts it is
    part of, the pieces most to the point (each with its place and its PDF pages, word for word), then the accounts
    of other rubrics they name. ``code``: an account (6125, or a dossier's own 61251001); ``words``: the question's
    words (« charges constatées d'avance »). Either or both; neither raises ToolError."""
    if not (str(code).strip() or str(words).strip()):
        raise ToolError("Donnez un compte (6125) ou des mots (charges constatées d'avance) à chercher dans le CGNC.")
    with cabinet_db() as db:
        found = search(db, code, words)
    asked = " ".join(part for part in (str(code).strip(), str(words).strip()) if part)
    if not found["pieces"]:
        return f"Le CGNC ne dit rien sur « {asked} »."
    lines = []
    if found["account"]:
        lines.append("Compte : " + " › ".join(f"{one['code']} {one['label']}" for one in found["account"]))
    for piece in found["pieces"]:
        lines += ["", f"--- CGNC, {piece['place']} (p. {piece['pages']} du PDF)", piece["body"]]
    if found["linked"]:
        lines += ["", "Comptes cités : " + " ; ".join(f"{one['code']} {one['label']}" for one in found["linked"])]
    return "\n".join(lines)
