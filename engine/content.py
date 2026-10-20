"""What Tarik says and is told, kept apart from the code so the team can read and change it from the UI: the agent's
definition (agent.json: model, instructions, tools' descriptions, read tools), its skills (skills/*.md), the tools'
prompts to the model (tools/*.md) and the UI's plain words (labels.json: names and descriptions of each workflow,
step and status).

content/ holds each one's first version. Every change made in the UI is a new version, kept with who made it, when
and why (table content_versions, schema orchestrator); the latest is the one in use, read at each use, so a change
applies to the next run without a restart. Going back is one more version, equal to an older one: nothing is lost.
A change that would break what reads it is refused (checked()): content, not logic."""

import json
import re
from pathlib import Path

import tarik.core.model

CONTENT = Path(__file__).resolve().parents[1] / "content"
FIRST = "Version du dépôt"  # who wrote the versions taken from content/
TABLE = """
create table if not exists content_versions (
    id bigserial primary key,
    path text not null,
    text text not null,
    author text not null,
    note text not null default '',
    created_at timestamptz not null default now()
);
create index if not exists content_versions_path on content_versions (path, id desc);
"""
LATEST = {}  # path: its latest text (this process serves every edit, so this is always current)


def documents():
    """Every editable document's path, relative to content/."""
    return sorted(
        str(path.relative_to(CONTENT))
        for pattern in ("*.json", "skills/*.md", "tools/*.md")
        for path in CONTENT.glob(pattern)
    )


def text(path):
    """A document's latest text."""
    if path not in LATEST:
        LATEST[path] = (CONTENT / path).read_text()  # before the store is open (tests, scripts)
    return LATEST[path]


# The two JSON documents, as dicts read everywhere; changed in place by a new version, so every reader sees it.
AGENT = json.loads(text("agent.json"))
LABELS = json.loads(text("labels.json"))
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


async def setup(pool):
    """The versions' table, each document's first version taken from content/ when it has none, and the latest of
    each put to use."""
    async with pool.connection() as db:
        await db.execute(TABLE)
        for path in documents():
            found = await (
                await db.execute("select text from content_versions where path = %s order by id desc limit 1", (path,))
            ).fetchone()
            if found is None:
                first = (CONTENT / path).read_text()
                await db.execute(
                    "insert into content_versions (path, text, author) values (%s, %s, %s)", (path, first, FIRST)
                )
                found = (first,)
            LATEST[path] = found[0]
            applied(path)


async def save(pool, path, new, author, note=""):
    """``new`` as the document's latest version, by ``author``; ValueError (in plain French) when it would break
    what reads it. Nothing is saved when it is the same as the latest."""
    if path not in documents():
        raise ValueError("document inconnu")
    checked(path, new)
    if new == text(path):
        return False
    async with pool.connection() as db:
        await db.execute(
            "insert into content_versions (path, text, author, note) values (%s, %s, %s, %s)",
            (path, new, author, note.strip()[:300]),
        )
    LATEST[path] = new
    applied(path)
    return True


async def history(pool, path, limit=50):
    """The document's versions, latest first: {id, author, note, at}."""
    async with pool.connection() as db:
        rows = await (
            await db.execute(
                "select id, author, note, created_at from content_versions where path = %s order by id desc limit %s",
                (path, limit),
            )
        ).fetchall()
    return [{"id": i, "author": author, "note": note, "at": at} for i, author, note, at in rows]


async def version(pool, path, version_id):
    """One version's text, and the one before it (None for the first): (text, previous)."""
    async with pool.connection() as db:
        rows = await (
            await db.execute(
                "select text from content_versions where path = %s and id <= %s order by id desc limit 2",
                (path, version_id),
            )
        ).fetchall()
    if not rows:
        return None, None
    return rows[0][0], rows[1][0] if len(rows) > 1 else None


async def last_changes(pool):
    """{path: {author, at}} of each document's latest version."""
    async with pool.connection() as db:
        rows = await (
            await db.execute(
                "select distinct on (path) path, author, created_at from content_versions order by path, id desc"
            )
        ).fetchall()
    return {path: {"author": author, "at": at} for path, author, at in rows}


# --- What a version must keep ---


def front_matter(text_):
    """A Markdown document's front matter ({key: value}) and body; ValueError without one."""
    found = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text_, flags=re.S)
    if not found:
        raise ValueError("l'en-tête (entre les deux lignes ---) a disparu")
    meta = dict(line.split(": ", 1) for line in found.group(1).splitlines() if ": " in line)
    return meta, found.group(2)


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


def checked(path, new):
    """ValueError, in plain French, when ``new`` would break what reads the document ``path``."""
    old = text(path)
    if path.endswith(".json"):
        try:
            parsed = json.loads(new)
        except ValueError:
            raise ValueError("ce n'est plus un JSON valide") from None
        same_shape(json.loads(old), parsed)
        if path == "agent.json" and parsed["model"] != json.loads(old)["model"]:
            raise ValueError("le modèle ne se modifie pas ici")
        if path == "agent.json":
            words = parsed["instructions"] + " " + " ".join(parsed["tools"].values())
            if wrong := unknown_names(words):
                raise ValueError(f"nomme des outils ou champs que Tarik n'a pas : {', '.join(wrong)}")
        return
    meta, body = front_matter(new)
    old_meta, _ = front_matter(old)
    if not body.strip():
        raise ValueError("le texte est vide")
    if set(meta) != set(old_meta) or any(meta[key] != old_meta[key] for key in old_meta if key != "description"):
        raise ValueError("l'en-tête ne se modifie pas ici, sauf la description")
    if not meta.get("description", "x").strip():
        raise ValueError("la description est vide")
    if path.startswith("skills/") and (wrong := unknown_names(body + " " + meta.get("description", ""))):
        raise ValueError(f"nomme des outils ou champs que Tarik n'a pas : {', '.join(wrong)}")
