"""What Tarik says and is told, kept apart from the code so the team can read and change it from the UI: the agent's
definition (agent.json: model, instructions, tools' descriptions, read tools), its skills (skills/*.md), the tools'
prompts to the model (tools/*.md) and the UI's plain words (labels.json: names and descriptions of each workflow,
step and status).

Postgres is their only home (table content_versions, schema orchestrator): every change made in the UI is a new
version, kept with who made it, when and why; the latest is the one in use, read at each use, so a change applies to
the next run without a restart. Going back is one more version, equal to an older one: nothing is lost. Moving
content between stacks, or filling a new one, is an export and an import (the Contenu page). A change that would
break what reads it is refused (checked()): content, not logic."""

import json
import re

import psycopg

import tarik.core.model
from tarik.core.config import POSTGRES
from tarik.core.model import front_matter
from tarik.core.queries import Q

PATH = re.compile(r"(agent|labels)\.json|(skills|tools)/[a-z0-9-]+\.md")  # the documents there can be
LATEST = {}  # path: its latest text (this process serves every edit, so this is always current)


def documents():
    """Every document's path (agent.json, skills/<name>.md...)."""
    return sorted(LATEST)


def text(path):
    """A document's latest text; LookupError when the stack has none (import the content first)."""
    if path not in LATEST:
        raise LookupError(f"contenu absent : {path} (importez le contenu, page Contenu)")
    return LATEST[path]


# The two JSON documents, as dicts read everywhere; filled and changed in place by each latest version.
AGENT, LABELS = {}, {}
tarik.core.model.SOURCE = text  # the tools read their prompts and the model's name from here


def applied(path):
    """A new latest version put to use: the JSON documents' dicts, and the canvases drawn from the labels."""
    if path == "agent.json":
        AGENT.clear()
        AGENT.update(json.loads(LATEST[path]))
    elif path == "labels.json":
        LABELS.clear()
        LABELS.update(json.loads(LATEST[path]))
        from engine import graphs  # graphs reads these labels: imported here, after it

        graphs.shape.cache_clear()


def loaded(rows):
    for path, found in rows:
        LATEST[path] = found
        applied(path)


async def setup(pool):
    """The versions' table, and each document's latest version put to use."""
    async with pool.connection() as db:
        loaded(await (await db.execute(Q.content_latest)).fetchall())


def load():
    """The latest versions read once, outside the server (check.py, the tests), from the stack's Postgres."""
    with psycopg.connect(**POSTGRES, options="-c search_path=orchestrator") as db:
        loaded(db.execute(Q.content_latest).fetchall())


async def save(pool, path, new, author, note="", create=False, reshape=False):
    """``new`` as the document's latest version, by ``author``; ValueError (in plain French) when it would break
    what reads it. Nothing is saved when it is the same as the latest. ``create``: a document the stack does not have
    yet may be made (an import into a new stack). ``reshape``: its sections may change, for code that adds or
    removes what they describe (a new step's name); never from the pages."""
    if path not in LATEST and not (create and PATH.fullmatch(path)):
        raise ValueError(f"document inconnu : {path}")
    checked(path, new, reshape)
    if new == LATEST.get(path):
        return False
    async with pool.connection() as db:
        await db.execute(
            Q.content_saved,
            (path, new, author, note.strip()[:300]),
        )
    LATEST[path] = new
    applied(path)
    from app import audit  # audit uses the run store, which imports the graphs, which read this content

    await audit.record(author, "content_saved", path, note=note.strip()[:300])
    return True


async def retire(pool, path, author, note):
    """A document no code reads any more, retired: an empty last version, so it is no longer one (content_latest),
    its history kept. Code only, when the code that read it goes."""
    async with pool.connection() as db:
        await db.execute(Q.content_saved, (path, "", author, note.strip()[:300]))
    LATEST.pop(path, None)


async def history(pool, path, limit=50):
    """The document's versions, latest first: {id, author, note, at}."""
    async with pool.connection() as db:
        rows = await (
            await db.execute(
                Q.content_history,
                (path, limit),
            )
        ).fetchall()
    return [{"id": i, "author": author, "note": note, "at": at} for i, author, note, at in rows]


async def version(pool, path, version_id):
    """One version's text, and the one before it (None for the first): (text, previous)."""
    async with pool.connection() as db:
        rows = await (
            await db.execute(
                Q.content_version,
                (path, version_id),
            )
        ).fetchall()
    if not rows:
        return None, None
    return rows[0][0], rows[1][0] if len(rows) > 1 else None


async def last_changes(pool):
    """{path: {author, at}} of each document's latest version."""
    async with pool.connection() as db:
        rows = await (await db.execute(Q.content_last_changes)).fetchall()
    return {path: {"author": author, "at": at} for path, author, at in rows}


# --- What a version must keep ---


def same_shape(old, new, where=""):
    """ValueError unless ``new`` has exactly ``old``'s keys, nested, with text where it had text."""
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
    """The tool and argument names ``words`` gives that the agent does not have: a skill naming a tool that does not
    exist sends the model to nothing. Names are snake_case words (Experio_find_dossier, dossier_id)."""
    from engine import agent

    known = {name for tool in agent.TOOLS for name in (tool.name, *tool.args)}
    return sorted(set(re.findall(r"\b[A-Za-z]+(?:_[a-z]+)+\b", words)) - known)


def checked(path, new, reshape=False):
    """ValueError, in plain French, when ``new`` would break what reads the document ``path``. A new document (an
    import into a new stack) has no earlier version to keep the shape of: only its own form is checked."""
    old = LATEST.get(path)
    if path.endswith(".json"):
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
            words = parsed["instructions"] + " " + " ".join(parsed["tools"].values())
            if wrong := unknown_names(words):
                raise ValueError(f"nomme des outils ou champs que Tarik n'a pas : {', '.join(wrong)}")
        return
    meta, body = front_matter(new)
    old_meta = front_matter(old)[0] if old is not None else meta
    if not body.strip():
        raise ValueError("le texte est vide")
    if set(meta) != set(old_meta) or any(meta[key] != old_meta[key] for key in old_meta if key != "description"):
        raise ValueError("l'en-tête ne se modifie pas ici, sauf la description")
    if not meta.get("description", "x").strip():
        raise ValueError("la description est vide")
    if path.startswith("skills/") and (wrong := unknown_names(body + " " + meta.get("description", ""))):
        raise ValueError(f"nomme des outils ou champs que Tarik n'a pas : {', '.join(wrong)}")
