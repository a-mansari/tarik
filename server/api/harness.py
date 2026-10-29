"""Serve the dashboard's map of Tarik's harness, read live from the code and the cabinet chosen in the sidebar: who
writes to him through which channel and conversation (and back), what he reaches himself (his model, his skills, his
tools, by family: his read tools and each workflow's) and where the tools act: Experio, or the CGNC (his RAG).

Settings are told only as set or not, never their values."""

import asyncio
import inspect
import re

from starlette.requests import Request

from engine import agent, content
from engine.knowledge import cgnc
from engine.tools.core.app import TOOLS
from engine.tools.core.cabinets import setting, using
from engine.tools.core.experio import dry_run
from engine.tools.core.model import ADAPTERS, adapter
from engine.tools.dossiers.routines import routines_of_cabinet, title_of
from server.api.content import GROUPS, PART_NAMES
from server.api.runs import chosen, page

RUNS = re.compile(r'run_workflow\(\s*"(\w+)"')  # the workflow an agent tool runs (engine/agent.py: run_workflow)


def first(text):
    """The first paragraph of ``text``, on one line."""
    return " ".join((text or "").strip().split("\n\n")[0].split())


def leads_to(found):
    """Where agent tool ``found`` leads: the workflow it runs, the CGNC for a read tool of engine/knowledge, else
    Experio (the read tools)."""
    if workflow := RUNS.search(inspect.getsource(found.coroutine or found.func)):
        return f"workflow:{workflow[1]}"
    name = found.name.removeprefix("Experio_")
    return "rag" if TOOLS[name].__module__.startswith("engine.knowledge") else "experio"


def harness(cabinet):
    """Return Tarik's harness for ``cabinet``: {nodes: [{id, kind, name, description, off, group}], edges:
    [{source, target, kind}]}. Kinds of node: source, channel, conversation, tarik, model, group (a family: the
    skills, the read tools, a workflow's tools), skill and tool (``group``: their family's id), end (where tools act);
    of edge: request (toward Tarik), reply (back to who wrote), uses (Tarik's own reach), leads (a tool to where it
    acts)."""
    with using(cabinet):
        chosen_model = adapter()
        mailbox = bool(setting("IMAP_USER", ""))
        testing = dry_run()
        routines = routines_of_cabinet()
    graphs, described = content.LABELS["graphs"], content.LABELS["descriptions"]["graphs"]
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
    node("email", "channel", graphs["emails"], f"{described.get('emails', '')}{no_mailbox}", off=not mailbox)
    node("chat", "channel", graphs["discussion"], described.get("discussion"))
    titles = "; ".join(title_of(r["id"], r["kind"], r["name"]) for r in routines) or "aucune pour l'instant"
    node("routine", "channel", graphs["routine"], f"{described.get('routine', '')} Routines du cabinet : {titles}.")
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

    # The tools by family, as Tarik capabilities lists them: his read tools, then each workflow's. Every one acts in
    # Experio but the CGNC's, searched in Tarik's own database.
    node("read", "group", PART_NAMES["outils/lecture"], "Les outils de Tarik qui lisent Experio, sans rien y changer.")
    for found in agent.described():
        if found.name == "load_skill":  # drawn as the skills themselves
            continue
        target = leads_to(found)
        family = target if target.startswith("workflow:") else "read"
        if family != "read" and not any(n["id"] == family for n in nodes):
            key = family.removeprefix("workflow:")
            node(family, "group", graphs.get(key, key), described.get(key))
        node(f"tool:{found.name}", "tool", found.name, found.description, group=family)
        edge("tarik", f"tool:{found.name}")
        edge(f"tool:{found.name}", "rag" if target == "rag" else "experio", "leads")
    mode = "En mode test : rien n'y est écrit." if testing else "En production : Tarik y écrit."
    node("experio", "end", "Experio", mode)
    node("rag", "end", "CGNC", cgnc.__doc__)
    return {"nodes": nodes, "edges": edges}


async def harness_page(request: Request):
    """GET /ui/api/harnais: return the harness map of the cabinet chosen in the sidebar, {cabinet, nodes, edges}."""
    cabinet = chosen(request)
    found = await asyncio.to_thread(harness, cabinet) if cabinet else {"nodes": [], "edges": []}
    return page({"cabinet": cabinet, **found})
