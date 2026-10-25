"""Each workflow's shape for the UI's canvas: its nodes, nested parts (a file, an invoice) as groups, and its edges,
the conditional ones marked, with the plain words of content/labels.json. Read from the compiled graphs themselves
(LangGraph's get_graph, xray: nested parts drawn whole), so the drawing never drifts from what runs."""

import collections
import functools

from langchain_anthropic import ChatAnthropic

from engine import agent, chat, declarations, dossier, emails, exports, traitement
from engine.content import LABELS

COMPILED = {
    "traitement": traitement.graph,
    "dossier": dossier.graph,
    "declarations": declarations.graph,
    "export": exports.graph,
    "emails": emails.graph,
    "fil": emails.FIL,
    "discussion": chat.graph,
}
# How deep each canvas opens nested graphs: Emails' follow-up runs Traitement (its own canvas), drawn as one box.
XRAY = {"emails": 1}
ENDS = {"__start__": "Début", "__end__": "Fin"}


def label(graph, node_id, table=None, fallback=None):
    """A node's plain name (or, from the descriptions table, what it does): its own graph's words for it, else
    another graph's (an email follow-up runs Traitement)."""
    name = node_id.rsplit(":", 1)[-1]
    if name in ENDS:
        return ENDS[name] if table is None else ""
    table = LABELS["steps"] if table is None else table
    for scope in (graph, *table):
        if name in table.get(scope, {}):
            return table[scope][name]
    return name if fallback is None else fallback


def description(graph, node_id):
    return label(graph, node_id, LABELS["descriptions"]["steps"], "")


def part_of(node_id):
    """The nested part a node is in ("fichier:facture" for "fichier:facture:lecture"), None at the top."""
    return node_id.rsplit(":", 1)[0] if ":" in node_id else None


def drawn_node(graph, node_id, parent, kind):
    return {
        "id": node_id,
        "label": label(graph, node_id),
        "description": description(graph, node_id),
        "parent": parent,
        "kind": kind,
    }


def hidden(edge, leaving, arriving):
    """Whether a conditional edge is kept off the canvas (the legend says it): a run that went along one shows it.
    A step that is not ok ends its invoice or file: drawn, those paths would converge from every step on the part's
    end, so where the step has another way on they are hidden. And a part may start at any of its steps (an invoice
    resumes where an answer leaves it): only its first entry is drawn, the step nothing else leads to; the others
    would fan out from its start."""
    if not edge.conditional:
        return False
    to_end = edge.target.endswith(":__end__") and leaving[edge.source] > 1
    from_start = edge.source.endswith(":__start__") and arriving[edge.target] > 1
    return to_end or from_start


@functools.cache
def shape(name):
    """{nodes: [{id, label, description, parent, kind}], edges: [{id, source, target, conditional}]} for graph
    ``name``. A nested part's nodes have their part as parent (a group); kind is "end" for a start or an end,
    "group", or "step"."""
    if name == "agent":
        compiled = agent.make_agent(chat=ChatAnthropic(model="drawing", api_key="unused"))
    else:
        compiled = COMPILED[name]
    drawn = compiled.get_graph(xray=XRAY.get(name, True))

    nodes, groups = [], {}
    for node_id in drawn.nodes:
        parent = part_of(node_id)
        prefix = parent
        while prefix:  # every enclosing part becomes a group, its own parent the part around it
            groups.setdefault(prefix, part_of(prefix))
            prefix = groups[prefix]
        kind = "end" if node_id.rsplit(":", 1)[-1] in ENDS else "step"
        nodes.append(drawn_node(name, node_id, parent, kind))
    nodes += [drawn_node(name, group, parent, "group") for group, parent in groups.items()]

    leaving = collections.Counter(edge.source for edge in drawn.edges)
    arriving = collections.Counter(edge.target for edge in drawn.edges)
    edges = [
        {
            "id": f"{edge.source}->{edge.target}",
            "source": edge.source,
            "target": edge.target,
            "conditional": bool(edge.conditional),
            "hidden": hidden(edge, leaving, arriving),
        }
        for edge in drawn.edges
    ]
    return {
        "graph": name,
        "label": LABELS["graphs"].get(name, name),
        "description": LABELS["descriptions"]["graphs"].get(name, ""),
        "nodes": nodes,
        "edges": edges,
    }
