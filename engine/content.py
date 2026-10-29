"""Tarik's content, kept apart from the code so the team can read and change it from the UI.

The documents: agent.json (model, instructions, tool descriptions, read tools), skills/*.md, the tools' prompts to
the model (tools/*.md) and labels.json (the UI's names and descriptions of each workflow, step and status).

Postgres is their only store (table content_versions, schema orchestrator). Every change made in the UI is a new
version, kept with its author, time and note; the latest is in use and read at each use, so a change applies to the
next run without a restart. Going back saves one more version equal to an older one: nothing is lost. Moving content
between stacks is an export and an import (the Contenu page). A change that would break the code reading the
document is refused (checked()): the team edits text, not logic."""

import json
import re

import psycopg

import engine.tools.core.model
from engine.tools.core.config import POSTGRES
from engine.tools.core.model import front_matter
from engine.tools.core.queries import Q

PATH = re.compile(r"(agent|labels)\.json|(skills|tools)/[a-z0-9-]+\.md")  # the documents there can be
LATEST = {}  # path: its latest text (this process serves every edit, so this is always current)


def documents():
    """Return every document's path, sorted (agent.json, skills/<name>.md...)."""
    return sorted(LATEST)


def text(path):
    """Return the document's latest text.

    Raises:
        LookupError: the stack has no such document (the content was never imported).
    """
    if path not in LATEST:
        raise LookupError(f"contenu absent : {path} (importez le contenu, page Contenu)")
    return LATEST[path]


# The two JSON documents, as dicts read everywhere; filled and changed in place by each latest version.
AGENT, LABELS = {}, {}
engine.tools.core.model.SOURCE = text  # the tools read their prompts and the model's name from here


def applied(path):
    """Apply the new latest version of ``path``: refill AGENT or LABELS in place and, for labels.json, clear the
    cached canvases (graphs.shape)."""
    if path == "agent.json":
        AGENT.clear()
        AGENT.update(json.loads(LATEST[path]))
    elif path == "labels.json":
        LABELS.clear()
        LABELS.update(json.loads(LATEST[path]))
        from engine import graphs  # graphs reads these labels: imported here, after it

        graphs.shape.cache_clear()


def loaded(rows):
    """Store and apply each (path, text) row as that document's latest version."""
    for path, found in rows:
        LATEST[path] = found
        applied(path)


async def setup(pool):
    """Read each document's latest version from Postgres (``pool``) and apply it; the server calls this at start."""
    async with pool.connection() as db:
        loaded(await (await db.execute(Q.content_latest)).fetchall())


def load():
    """Read and apply each document's latest version once, outside the server (check.py, the tests)."""
    with psycopg.connect(**POSTGRES, options="-c search_path=orchestrator") as db:
        loaded(db.execute(Q.content_latest).fetchall())


async def save(pool, path, new, author, note="", create=False, reshape=False):
    """Save ``new`` as the latest version of ``path`` and apply it; return True when saved, False when unchanged.

    Args:
        author: who made the change (kept with the version and in the journal).
        note: why, trimmed to 300 characters.
        create: allow a document the stack does not have yet (an import into a new stack).
        reshape: allow the JSON sections to change, for code that adds or removes what they describe (a new step's
            name); never from the pages.

    Raises:
        ValueError: (in plain French) an unknown path, or ``new`` would break what reads it (checked()).
    """
    if path not in LATEST and not (create and PATH.fullmatch(path)):
        raise ValueError(f"document inconnu : {path}")
    checked(path, new, reshape)
    if new == LATEST.get(path):
        return False
    async with pool.connection() as db:
        await db.execute(Q.content_saved, (path, new, author, noted(note)))
    LATEST[path] = new
    applied(path)
    from server import audit  # audit uses the run store, which imports the graphs, which read this content

    await audit.record(author, "content_saved", path, note=noted(note))
    return True


def noted(note):
    """Return a version's note as kept: trimmed, 300 characters at most."""
    return note.strip()[:300]


async def retire(pool, path, author, note):
    """Retire a document no code reads any more: save an empty last version, so content_latest no longer lists it,
    and keep its history. Called from code only, when the code reading it is removed."""
    async with pool.connection() as db:
        await db.execute(Q.content_saved, (path, "", author, noted(note)))
    LATEST.pop(path, None)


async def history(pool, path, limit=50):
    """Return the document's last ``limit`` versions, latest first, as [{id, author, note, at}]."""
    async with pool.connection() as db:
        rows = await (await db.execute(Q.content_history, (path, limit))).fetchall()
    return [{"id": i, "author": author, "note": note, "at": at} for i, author, note, at in rows]


async def version(pool, path, version_id):
    """Return (text, previous) for version ``version_id`` of ``path``: previous is the version before it, None for
    the first; (None, None) when there is no such version."""
    async with pool.connection() as db:
        rows = await (await db.execute(Q.content_version, (path, version_id))).fetchall()
    if not rows:
        return None, None
    return rows[0][0], rows[1][0] if len(rows) > 1 else None


async def last_changes(pool):
    """Return {path: {author, at}} for each document's latest version."""
    async with pool.connection() as db:
        rows = await (await db.execute(Q.content_last_changes)).fetchall()
    return {path: {"author": author, "at": at} for path, author, at in rows}


# --- What a version must keep ---


def same_shape(old, new, where=""):
    """Raise ValueError unless ``new`` has exactly ``old``'s keys, nested, with non-empty text where ``old`` had text
    and equal values elsewhere. ``where`` is the key path, for the message."""
    if isinstance(old, dict):
        if not isinstance(new, dict) or set(old) != set(new):
            raise ValueError(f"les rubriques de {where or 'ce document'} ont changé : seul le texte se modifie ici")
        for key in old:
            same_shape(old[key], new[key], f"{where} › {key}" if where else key)
    elif isinstance(old, str):
        if not isinstance(new, str) or not new.strip():
            raise ValueError(f"{where} est vide")
    elif old != new:
        raise ValueError(f"{where} ne se modifie pas ici")


def unknown_names(words):
    """Return the snake_case names in ``words`` (Experio_find_dossier, dossier_id) that are no agent tool or tool
    argument, sorted. A skill naming a tool that does not exist sends the model nowhere."""
    from engine import agent

    known = {name for tool in agent.TOOLS for name in (tool.name, *tool.args)}
    return sorted(set(re.findall(r"\b[A-Za-z]+(?:_[a-z]+)+\b", words)) - known)


def names_known(words):
    """Raise ValueError when ``words`` name a tool or argument the agent does not have (unknown_names)."""
    wrong = unknown_names(words)
    if wrong:
        raise ValueError(f"nomme des outils ou champs que Tarik n'a pas : {', '.join(wrong)}")


def checked(path, new, reshape=False):
    """Raise ValueError, in plain French, when ``new`` would break the code reading document ``path``.

    A new document (an import into a new stack) has no earlier version to keep the shape of: only its own form is
    checked. ``reshape`` lets a JSON document's sections change."""
    old = LATEST.get(path)
    if path.endswith(".json"):
        checked_json(path, old, new, reshape)
    else:
        checked_markdown(path, old, new)


def checked_json(path, old, new, reshape):
    """checked() for a JSON document: valid JSON, the same shape (unless ``reshape``), agent.json's model unchanged
    and its required sections present, and the tool names its text gives known (names_known)."""
    try:
        parsed = json.loads(new)
    except ValueError:
        raise ValueError("ce n'est plus un JSON valide") from None
    if old is not None and not reshape:
        same_shape(json.loads(old), parsed)
        if path == "agent.json" and parsed["model"] != json.loads(old)["model"]:
            raise ValueError("le modèle ne se modifie pas ici")
    elif path == "agent.json" and not {"model", "instructions", "tools", "read_tools"} <= set(parsed):
        raise ValueError("il manque des rubriques (model, instructions, tools, read_tools)")
    if path == "agent.json":
        tools = {**parsed["tools"], **parsed.get("own_tools", {})}
        names_known(parsed["instructions"] + " " + " ".join(tools.values()))


def checked_markdown(path, old, new):
    """checked() for a skill or a tool's prompt: a non-empty body, the front matter unchanged except the description
    (which must stay non-empty), and for a skill only known tool names."""
    meta, body = front_matter(new)
    old_meta = front_matter(old)[0] if old is not None else meta
    if not body.strip():
        raise ValueError("le texte est vide")
    if set(meta) != set(old_meta) or any(meta[key] != old_meta[key] for key in old_meta if key != "description"):
        raise ValueError("l'en-tête ne se modifie pas ici, sauf la description")
    if not meta.get("description", "x").strip():
        raise ValueError("la description est vide")
    if path.startswith("skills/"):
        names_known(body + " " + meta.get("description", ""))
