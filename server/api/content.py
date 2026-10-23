"""The content pages: everything the team may change without touching code (engine/content.py), each document as a
plain form, its history (who, when, why), what each version changed, and going back to one in a click."""

import difflib
import json
import re
from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from engine import content, runs
from server.api.runs import graph_name, page

SEP = "|"  # a JSON field's path in a form: keys joined


def title(path):
    """A document's name, as the team reads it."""
    if path == "agent.json":
        return "Instructions de Tarik et ses outils"
    if path == "labels.json":
        return "Noms et descriptions des pages"
    meta, _ = content.front_matter(content.text(path))
    if path.startswith("skills/"):
        return f"Compétence « {meta.get('name', path)} »"
    return f"Consigne de lecture « {path.removeprefix('tools/').removesuffix('.md')} »"


def what(path):
    """What a document is for, in one sentence."""
    if path == "agent.json":
        return "Ce que Tarik sait de son rôle, et comment chaque outil lui est présenté. Lu à chaque message."
    if path == "labels.json":
        return "Les mots des pages : le nom et l'explication de chaque workflow, de chaque étape et de chaque état."
    if path.startswith("skills/"):
        return "Ce que Tarik suit pour un type de demande. Il charge la compétence quand sa description correspond."
    meta, _ = content.front_matter(content.text(path))
    return f"Ce que le modèle reçoit quand l'outil {meta.get('used_by', '').split(' ')[0]} lit un document."


GROUPS = {  # key: (name, which documents)
    "tarik": ("Tarik", lambda p: p == "agent.json"),
    "competences": ("Compétences", lambda p: p.startswith("skills/")),
    "consignes": ("Consignes de lecture des documents", lambda p: p.startswith("tools/")),
    "pages": ("Pages", lambda p: p == "labels.json"),
}


def groups():
    """The documents by kind, for the list page: [(key, name, paths)]."""
    paths = content.documents()
    return [(key, name, [p for p in paths if belongs(p)]) for key, (name, belongs) in GROUPS.items()]


def field(key, label, value, long=True, hint=""):
    return {"key": SEP.join(key), "label": label, "value": value, "long": long, "hint": hint}


def agent_sections(data):
    """agent.json's sections: Tarik's instructions, then how each tool is presented to it."""
    tools = [field(["tools", name], name, value) for name, value in data["tools"].items()]
    read_tools = [field(["read_tools", name], f"Experio_{name}", value) for name, value in data["read_tools"].items()]
    return [
        {"title": "Instructions", "fields": [field(["instructions"], "Instructions de Tarik", data["instructions"])]},
        {"title": "Outils de traitement", "fields": tools},
        {"title": "Outils de lecture d'Experio", "fields": read_tools},
    ]


def graph_fields(data, graph, name):
    """labels.json's fields of one workflow: its name and what it does, then each step's name and what it does."""
    fields = [
        field(["graphs", graph], "Nom du workflow", name, long=False),
        field(["descriptions", "graphs", graph], "Ce qu'il fait", data["descriptions"]["graphs"].get(graph, "")),
    ]
    for step, step_name in data["steps"].get(graph, {}).items():
        fields.append(field(["steps", graph, step], f"Étape {step} : nom", step_name, long=False, hint=step))
        described = data["descriptions"]["steps"].get(graph, {}).get(step)
        if described is not None:
            fields.append(field(["descriptions", "steps", graph, step], f"Étape {step} : ce qu'elle fait", described))
    return fields


def labels_sections(data):
    """labels.json's sections: one per workflow, then the states."""
    found = []
    for graph, name in data["graphs"].items():
        found.append({"title": graph_name(graph), "fields": graph_fields(data, graph, name)})
    statuses = [field(["statuses", key], key, value, long=False) for key, value in data["statuses"].items()]
    found.append({"title": "États", "fields": statuses})
    return found


def sections(path, text):
    """The document as form sections: [{title, fields: [{key, label, value, long, hint}]}]."""
    if path == "agent.json":
        return agent_sections(json.loads(text))
    if path == "labels.json":
        return labels_sections(json.loads(text))
    meta, body = content.front_matter(text)
    fields = [field(["body"], "Texte", body.strip("\n"))]
    if "description" in meta:
        fields.insert(0, field(["description"], "Quand Tarik la charge (sa description)", meta["description"]))
    return [{"title": "", "fields": fields}]


ROOTS = ("instructions", "tools", "read_tools", "graphs", "steps", "statuses", "descriptions")  # a JSON form's fields
MAX_FIELDS = 500


def rebuilt(path, form):
    """The document's new text from the form's fields ({key: text}), on top of its latest version."""
    if not isinstance(form, dict) or len(form) > MAX_FIELDS or not all(isinstance(v, str) for v in form.values()):
        raise ValueError("un formulaire illisible")
    values = {key: value.replace("\r\n", "\n") for key, value in form.items()}
    old = content.text(path)
    if path.endswith(".json"):
        data = json.loads(old)
        for key, value in values.items():
            if not key.startswith(ROOTS):
                continue
            *parents, last = key.split(SEP)
            target = data
            for part in parents:
                target = target[part]
            if last not in target:
                raise ValueError("un champ inconnu")
            target[last] = value.strip()
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    meta, body = content.front_matter(old)
    if "description" in values and "description" in meta:
        meta["description"] = " ".join(values["description"].split())
    body = values.get("body", body).strip("\n")
    end = "\n" if old.endswith("\n") else ""  # as the document was: a prompt may be followed by more text
    header = "\n".join(f"{key}: {value}" for key, value in meta.items())
    return "---\n" + header + "\n---\n" + body + end


CONTEXT = 40  # words kept around each change


def diff(before, after):
    """What changed between two texts, word by word: [(kind, text)], kind "+" (added), "-" (removed), " " (kept, cut
    to CONTEXT words around each change, "…" where cut)."""
    old, new = re.split(r"(\s+)", before), re.split(r"(\s+)", after)
    found = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            kept = old[i1:i2]
            if len(kept) > 4 * CONTEXT:  # long unchanged stretch: its ends only (tokens alternate word, space)
                head = "".join(kept[: 2 * CONTEXT]) if found else ""
                tail = "".join(kept[-2 * CONTEXT :]) if i2 < len(old) else ""
                found.append((" ", f"{head} … {tail}" if head or tail else ""))
            else:
                found.append((" ", "".join(kept)))
            continue
        if i2 > i1:
            found.append(("-", "".join(old[i1:i2])))
        if j2 > j1:
            found.append(("+", "".join(new[j1:j2])))
    return found


def changes(path, before, after):
    """{field key: {parts, before, after}} of each field one version changed, against the version before it (None:
    the first): its change word by word, and its length in characters before and after."""
    if before is None:
        return {}
    old = {f["key"]: f["value"] for section in sections(path, before) for f in section["fields"]}
    found = {}
    for section in sections(path, after):
        for f in section["fields"]:
            was = old.get(f["key"], "")
            if was != f["value"]:
                found[f["key"]] = {"parts": diff(was, f["value"]), "before": len(was), "after": len(f["value"])}
    return found


# --- Export and import: a whole group of documents, or one card's fields ---

BUNDLE, SECTION = "tarik-contenu", "tarik-contenu-section"
TIPS = {  # what each button is for, and its file's format, shown when it is hovered or focused
    "all_export": "Télécharge tout le contenu (instructions, compétences, consignes, pages) en un fichier JSON au "
    "format « tarik-contenu » : chaque document et son texte en service. Pour le garder en sauvegarde, ou le copier "
    "vers une autre installation.",
    "all_import": "Charge un fichier exporté ici (JSON « tarik-contenu », tout ou un groupe). Chaque document qui "
    "diffère devient une nouvelle version, notée « Importé de … » ; un document absent est créé. Tout est vérifié "
    "avant : un seul refusé, rien n'est enregistré.",
    "group_export": "Télécharge les documents « {group} » en un fichier JSON au format « tarik-contenu » : chaque "
    "document et son texte en service. Pour les garder, ou les copier vers une autre installation.",
    "group_import": "Charge un fichier exporté (JSON « tarik-contenu ») : chaque document qui diffère devient une "
    "nouvelle version. Tout est vérifié avant : un seul refusé, rien n'est enregistré.",
    "section_export": "Télécharge les champs de cette section en un fichier JSON au format « tarik-contenu-section » "
    ": le document, la section, et chaque champ avec son texte. Modifiable, puis réimportable ici.",
    "section_import": "Charge un fichier exporté de cette même section (JSON « tarik-contenu-section »). Ses champs "
    "remplacent ceux en service et forment une nouvelle version, vérifiée avant d'être enregistrée.",
}


def download(data, name):
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return Response(
        body, media_type="application/json", headers={"content-disposition": f'attachment; filename="{name}"'}
    )


async def export_contents(request: Request):
    """Every document's latest version as one file, or only a group's (?group=): to keep, or to fill another stack."""
    key = request.query_params.get("group")
    belongs = GROUPS[key][1] if key in GROUPS else (lambda path: True)
    documents = {path: content.text(path) for path in content.documents() if belongs(path)}
    data = {"format": BUNDLE, "exported_at": datetime.now(UTC).isoformat(timespec="seconds"), "documents": documents}
    group = f"-{key}" if key in GROUPS else ""
    return download(data, f"tarik-contenu{group}-{datetime.now(UTC):%Y%m%d}.json")


async def import_contents(request: Request):
    """An export's documents, each as a new version (made when the stack lacks it). Every document is checked before
    any is saved: one refused, none saved. {saved: [...], unchanged: [...]} or {error}."""
    try:
        data = json.loads(await request.body())
        documents = data["documents"]
        name = str(data.get("file") or "un export")[:80]
        if data.get("format") != BUNDLE or not isinstance(documents, dict):
            raise ValueError("ce fichier n'est pas un export du contenu de Tarik")
        for path, text in documents.items():
            if not isinstance(text, str) or not content.PATH.fullmatch(path):
                raise ValueError(f"document inconnu : {path}")
            try:
                content.checked(path, text, reshape=True)  # a newer stack's sections (a new tool's description)
            except ValueError as refusal:
                raise ValueError(f"{path} : {refusal}") from None
    except (ValueError, KeyError, TypeError) as refusal:
        return JSONResponse({"error": str(refusal)}, status_code=400)
    saved, unchanged = [], []
    for path, text in sorted(documents.items()):
        made = await content.save(
            runs.pool, path, text, author_of(request), f"Importé de {name}", create=True, reshape=True
        )
        if made:
            saved.append(path)
        else:
            unchanged.append(path)
    return JSONResponse({"saved": saved, "unchanged": unchanged})


def author_of(request):
    user = request.state.user
    return f"{user['name']} <{user['email']}>"


async def import_section(request, path, data):
    """One card's fields from its export, over the document's latest version: a new version, or {error}."""
    try:
        if data.get("format") != SECTION or data.get("document") != path or not isinstance(data.get("fields"), dict):
            raise ValueError("ce fichier n'est pas l'export d'une section de ce document")
        form = {f["key"]: f["value"] for section in sections(path, content.text(path)) for f in section["fields"]}
        unknown = [key for key in data["fields"] if key not in form]
        if unknown:
            raise ValueError(f"champs inconnus : {', '.join(unknown)}")
        form |= {key: str(value) for key, value in data["fields"].items()}
        note = f"Importé : {data.get('section') or 'une section'} ({str(data.get('file') or 'un export')[:80]})"
        made = await content.save(runs.pool, path, rebuilt(path, form), author_of(request), note)
    except (ValueError, KeyError, TypeError) as refusal:
        return JSONResponse({"error": str(refusal)}, status_code=400)
    return JSONResponse({"saved": made})


async def contents_page(request: Request):
    last = await content.last_changes(runs.pool)
    listed = []
    for key, name, paths in groups():
        documents = [{"path": path, "title": title(path), "change": shown(last.get(path))} for path in paths]
        listed.append({"key": key, "name": name, "documents": documents})
    return page({"groups": listed, "empty": not content.documents(), "tips": TIPS})


def shown(change):
    """A version's author (their name only) and time, as the pages read them; None for none."""
    if change is None:
        return None
    return {**change, "author": change["author"].split(" <")[0], "at": runs.when(change["at"])}


def card_download(path, text, index):
    """Card ``index`` of the document (its fields) as a file to download; None when it has no such card."""
    cards = sections(path, text)
    if index >= len(cards):
        return None
    card = cards[index]
    fields = {f["key"]: f["value"] for f in card["fields"]}
    data = {"format": SECTION, "document": path, "section": card["title"] or title(path), "fields": fields}
    stem = path.replace("/", "-").rsplit(".", 1)[0]
    return download(data, f"{stem}-{index + 1}.json")


async def compared(path, shown):
    """What version ``shown`` (its id, as text) changed, for the page: {id, first, fields}; None for no such version."""
    if not shown or not shown.isdigit():
        return None
    after, before = await content.version(runs.pool, path, int(shown))
    if after is None:
        return None
    return {"id": int(shown), "first": before is None, "fields": changes(path, before, after)}


async def content_page(request: Request):
    """A document's form (GET), saved (POST {fields, note}), one of its versions restored (POST {action: "restore",
    version, note}), or one card's fields imported (POST its export): {saved} or {error}."""
    path = request.path_params["path"]
    if path not in content.documents():
        return page({"error": "document inconnu"}, 404)
    text = content.text(path)
    exported = request.query_params.get("export")
    if exported is not None and exported.isdigit():  # one card's fields, as a file
        card = card_download(path, text, int(exported))
        if card is not None:
            return card
    if request.method == "POST":
        return await saved(request, path)
    versions = [shown(v) for v in await content.history(runs.pool, path)]
    context = {
        "path": path,
        "title": title(path),
        "what": what(path),
        "sections": sections(path, text),
        "versions": versions,
        "compare": await compared(path, request.query_params.get("version")),
        "model": json.loads(text)["model"] if path == "agent.json" else None,
        "tips": TIPS,
    }
    return page(context)


async def saved(request, path):
    """The form's fields as a new version, or an old version again: {saved: whether a version was made} or {error}."""
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("un formulaire illisible")
    except ValueError as refusal:
        return JSONResponse({"error": str(refusal)}, status_code=400)
    if "format" in data:
        return await import_section(request, path, data)
    note = str(data.get("note") or "")
    try:
        if data.get("action") == "restore":
            old, _ = await content.version(runs.pool, path, int(data["version"]))
            if old is None:
                raise ValueError("version inconnue")
            note = note or f"Retour à la version {data['version']}"
            new = old
        else:
            new = rebuilt(path, data.get("fields"))
        changed = await content.save(runs.pool, path, new, author_of(request), note)
    except (ValueError, KeyError, TypeError) as refusal:
        return JSONResponse({"error": str(refusal)}, status_code=400)
    return JSONResponse({"saved": changed})
