"""The content pages: everything the team may change without touching code (engine/content.py), each document as a
plain form, its history (who, when, why), what each version changed, and going back to one in a click."""

import contextlib
import difflib
import json
from urllib.parse import parse_qs, quote

from starlette.requests import Request
from starlette.responses import RedirectResponse

from engine import content, runs
from ui.pages import graph_name, templates

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


def groups():
    """The documents by kind, for the list page."""
    paths = content.documents()
    return [
        ("Tarik", [p for p in paths if p == "agent.json"]),
        ("Compétences", [p for p in paths if p.startswith("skills/")]),
        ("Consignes de lecture des documents", [p for p in paths if p.startswith("tools/")]),
        ("Pages", [p for p in paths if p == "labels.json"]),
    ]


def field(key, label, value, long=True, hint=""):
    return {"key": SEP.join(key), "label": label, "value": value, "long": long, "hint": hint}


def sections(path, text):
    """The document as form sections: [{title, fields: [{key, label, value, long, hint}]}]."""
    if path == "agent.json":
        data = json.loads(text)
        return [
            {
                "title": "Instructions",
                "fields": [field(["instructions"], "Instructions de Tarik", data["instructions"])],
            },
            {
                "title": "Outils de traitement",
                "fields": [field(["tools", name], name, value) for name, value in data["tools"].items()],
            },
            {
                "title": "Outils de lecture d'Experio",
                "fields": [
                    field(["read_tools", name], f"Experio_{name}", value) for name, value in data["read_tools"].items()
                ],
            },
        ]
    if path == "labels.json":
        data = json.loads(text)
        found = []
        for graph, name in data["graphs"].items():
            fields = [
                field(["graphs", graph], "Nom du workflow", name, long=False),
                field(
                    ["descriptions", "graphs", graph], "Ce qu'il fait", data["descriptions"]["graphs"].get(graph, "")
                ),
            ]
            for step, step_name in data["steps"].get(graph, {}).items():
                fields.append(field(["steps", graph, step], f"Étape {step} : nom", step_name, long=False, hint=step))
                described = data["descriptions"]["steps"].get(graph, {}).get(step)
                if described is not None:
                    fields.append(
                        field(["descriptions", "steps", graph, step], f"Étape {step} : ce qu'elle fait", described)
                    )
            found.append({"title": graph_name(graph), "fields": fields})
        found.append(
            {
                "title": "États",
                "fields": [field(["statuses", key], key, value, long=False) for key, value in data["statuses"].items()],
            }
        )
        return found
    meta, body = content.front_matter(text)
    fields = [field(["body"], "Texte", body.strip("\n"))]
    if "description" in meta:
        fields.insert(0, field(["description"], "Quand Tarik la charge (sa description)", meta["description"]))
    return [{"title": "", "fields": fields}]


def rebuilt(path, form):
    """The document's new text from the form's fields, on top of its latest version."""
    values = {key: values[0].replace("\r\n", "\n") for key, values in form.items()}
    old = content.text(path)
    if path.endswith(".json"):
        data = json.loads(old)
        for key, value in values.items():
            if not key.startswith(
                ("instructions", "tools", "read_tools", "graphs", "steps", "statuses", "descriptions")
            ):
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
    return "---\n" + "\n".join(f"{key}: {value}" for key, value in meta.items()) + "\n---\n" + body + end


def diff(before, after):
    """[(kind, line)] of what changed, kind "+", "-" or " " (a few lines around each change)."""
    if before is None:
        return [(" ", "Première version.")]
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2)
    return [(line[:1], line[1:]) for line in list(lines)[2:] if not line.startswith("@@")] or [
        (" ", "Aucun changement.")
    ]


async def contents_page(request: Request):
    changes = await content.last_changes(runs.pool)
    listed = [(name, [(path, title(path), changes.get(path)) for path in paths]) for name, paths in groups()]
    return templates.TemplateResponse(request, "contents.html", {"groups": listed})


async def content_page(request: Request):
    """A document's form (GET), saved (POST), or one of its versions restored (POST action=restore)."""
    path = request.path_params["path"]
    if path not in content.documents():
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    user = request.state.user
    author = f"{user['name']} <{user['email']}>"
    error, text = None, content.text(path)
    if request.method == "POST":
        form = parse_qs((await request.body()).decode(), keep_blank_values=True, max_num_fields=500)
        note = form.pop("note", [""])[0]
        try:
            if form.get("action", [""])[0] == "restore":
                old, _ = await content.version(runs.pool, path, int(form["version"][0]))
                if old is None:
                    raise ValueError("version inconnue")
                note = note or f"Retour à la version {form['version'][0]}"
                new = old
            else:
                new = rebuilt(path, {key: value for key, value in form.items() if key != "action"})
            changed = await content.save(runs.pool, path, new, author, note)
            return RedirectResponse(f"/ui/contenu/{quote(path)}?saved={'1' if changed else '0'}", status_code=303)
        except (ValueError, KeyError) as refusal:
            error = str(refusal)
            if form.get("action", [""])[0] != "restore":
                with contextlib.suppress(ValueError, KeyError):
                    text = rebuilt(path, form)  # the form shown again as it was sent, to be corrected
    versions = await content.history(runs.pool, path)
    shown = request.query_params.get("version")
    compare = None
    if shown and shown.isdigit():
        after, before = await content.version(runs.pool, path, int(shown))
        if after is not None:
            compare = {"id": int(shown), "lines": diff(before, after)}
    context = {
        "path": path,
        "title": title(path),
        "what": what(path),
        "sections": sections(path, text),
        "versions": versions,
        "compare": compare,
        "error": error,
        "saved": request.query_params.get("saved"),
        "model": json.loads(text)["model"] if path == "agent.json" else None,
    }
    return templates.TemplateResponse(request, "content.html", context, status_code=400 if error else 200)
