"""Serve the dashboard's map of Tarik's harness, read live from the code and the cabinet chosen in the sidebar: who
writes to him through which channel and conversation (and back), what he reaches himself (his model, his skills, his
tools, by family: his read tools, each workflow's, his own base's) and where the tools act: Experio, the CGNC (his
RAG) or his own base.

Settings are told only as set or not, never their values; a cabinet's routines named to its admins only, as on the
Routines page (server.py: admin_only)."""

import asyncio
import functools
import inspect
import re

from starlette.requests import Request

from engine import agent, content
from engine.tools.core.app import TOOLS
from engine.tools.core.cabinets import setting, using
from engine.tools.core.experio import dry_run
from engine.tools.core.model import ADAPTERS, adapter
from engine.tools.dossiers.routines import routines_of_cabinet, title_of
from server.api.content import GROUPS, PART_NAMES
from server.api.runs import chosen, graph_name, is_admin, page

RUNS = re.compile(r'run_workflow\(\s*"(\w+)"')  # the workflow an agent tool runs (engine/agent.py: run_workflow)


def first(text):
    """The first paragraph of ``text``, on one line."""
    return " ".join((text or "").strip().split("\n\n")[0].split())


@functools.cache  # the code only changes with a restart
def workflow_in(function):
    """Return the workflow agent tool ``function``'s source runs (run_workflow("x"), its first), else None."""
    found = RUNS.search(inspect.getsource(function))
    return found and found[1]


def leads_to(found):
    """Return agent tool ``found``'s (family, end): a read tool (agent.READ_TOOLS) reads Experio, or the CGNC for one
    of engine/knowledge; a Tarik_ tool changes Tarik's own base; any other runs a workflow, which acts in Experio.
    Raises ValueError for a tool that runs no workflow its source names."""
    name = found.name.removeprefix("Experio_")
    if name in agent.READ_TOOLS:
        return "read", "rag" if TOOLS[name].__module__.startswith("engine.knowledge") else "experio"
    if found.name.startswith("Tarik_"):
        return "own", "base"
    if workflow := workflow_in(found.coroutine or found.func):
        return f"workflow:{workflow}", "experio"
    raise ValueError(f"{found.name}: no workflow named in its source")


def harness(cabinet, admin=False):
    """Return Tarik's harness for ``cabinet``: {nodes: [{id, kind, name, description, off, group}], edges:
    [{source, target, kind}]}. Kinds of node: source, channel, conversation, tarik, model, group (a family: the
    skills, the read tools, a workflow's tools, the own base's), skill and tool (``group``: their family's id), end
    (where tools act); of edge: request (toward Tarik), reply (back to who wrote), uses (Tarik's own reach), leads (a
    tool to where it acts). ``admin``: the routines are named, else only counted."""
    with using(cabinet):
        chosen_model = adapter()
        mailbox = bool(setting("IMAP_USER", ""))
        testing = dry_run()
        routines = routines_of_cabinet()
    described = content.LABELS["descriptions"]["graphs"]
    nodes, edges = [], []

    def node(key, kind, name, description="", off=False, group=None):
        nodes.append(
            {"id": key, "kind": kind, "name": name, "description": first(description), "off": off, "group": group}
        )

    def edge(source, target, kind="uses"):
        edges.append({"source": source, "target": target, "kind": kind})

    def both(source, target):
        """A request from ``source`` to ``target`` and the reply back."""
        edge(source, target, "request")
        edge(target, source, "reply")

    # Clients and the team alike write by email or on the Discussion page; a routine starts at its schedule. Tarik
    # only talks in conversations: each channel's message joins one, his reply going back the way it came (a
    # routine's report: by email).
    node("clients", "source", "Clients", "Les clients du cabinet.")
    node("team", "source", "Équipe Experio", "Les membres de l'équipe Experio.")
    node("clock", "source", "Horaire", "L'horaire de chaque routine (page Routines).")
    no_mailbox = "" if mailbox else " Aucune boîte configurée pour ce cabinet."
    node("email", "channel", graph_name("emails"), f"{described.get('emails', '')}{no_mailbox}", off=not mailbox)
    node("chat", "channel", graph_name("discussion"), described.get("discussion"))
    titles = "; ".join(title_of(r["id"], r["kind"], r["name"]) for r in routines) if admin else str(len(routines))
    listed = f" Routines du cabinet : {titles or 'aucune pour l’instant'}."
    node("routine", "channel", graph_name("routine"), f"{described.get('routine', '')}{listed}")
    node(
        "conversations",
        "conversation",
        "Conversations",
        "Une discussion, un fil d'emails ou une routine : ses messages et les résultats d'outils, gardés par LangGraph "
        "dans Postgres, renvoyés au modèle à chaque appel de cette conversation, jamais d'une autre.",
    )
    node("tarik", "tarik", "Tarik", described.get("agent"))
    for source in ("clients", "team"):
        both(source, "email")
        both(source, "chat")
    edge("clock", "routine", "request")
    for channel in ("email", "chat", "routine"):
        both(channel, "conversations")
    both("conversations", "tarik")

    node("model", "model", ADAPTERS[chosen_model], "Le modèle choisi pour ce cabinet (page Réglages).")
    edge("tarik", "model")
    node("skills", "group", GROUPS["competences"][0], "Ce que Tarik suit pour un type de demande (load_skill).")
    for name, description, _ in agent.skills():  # each loaded by load_skill, by its name
        node(f"skill:{name}", "skill", name, description, group="skills")
        edge("tarik", f"skill:{name}")

    # The tools by family, as Tarik capabilities lists them: his read tools, each workflow's, his own base's. Each acts
    # in Experio but the CGNC's (searched in Tarik's database) and the Tarik_ tools (his routines, his behaviour).
    families = {
        "read": (PART_NAMES["outils/lecture"], "Les outils de Tarik qui lisent, sans rien changer."),
        "own": (PART_NAMES["outils/base"], "Les outils qui changent ses routines et son comportement."),
    }
    for found in agent.described():
        if found.name == "load_skill":  # drawn as the skills themselves
            continue
        family, end = leads_to(found)
        if family not in {n["id"] for n in nodes}:
            key = family.removeprefix("workflow:")
            name, about = families.get(family) or (graph_name(key), described.get(key))
            node(family, "group", name, about)
        node(f"tool:{found.name}", "tool", found.name, found.description, group=family)
        edge("tarik", f"tool:{found.name}")
        edge(f"tool:{found.name}", end, "leads")
    mode = "En mode test : rien n'y est écrit." if testing else "En production : Tarik y écrit."
    node("experio", "end", "Experio", mode)
    node(
        "rag",
        "end",
        "CGNC",
        "Le Code général de normalisation comptable, mot pour mot, cherché par compte ou par mots dans la base "
        "de Tarik.",
    )
    node(
        "base",
        "end",
        "Base de Tarik",
        "Les routines et le comportement de Tarik pour ce cabinet. Le mode test ne s'y applique pas : ce qui y est "
        "changé l'est vraiment.",
    )
    return {"nodes": nodes, "edges": edges}


async def harness_page(request: Request):
    """GET /ui/api/harnais: return the harness map of the cabinet chosen in the sidebar, {cabinet, nodes, edges}."""
    cabinet = chosen(request)
    found = await asyncio.to_thread(harness, cabinet, is_admin(request)) if cabinet else {"nodes": [], "edges": []}
    return page({"cabinet": cabinet, **found})
