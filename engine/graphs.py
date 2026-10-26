"""Each workflow's shape for the UI's canvas: its nodes, its nested parts (a file, an invoice) as groups, and its
edges with the conditional ones marked, named with the plain words of content/labels.json.

The shape is read from the compiled graphs themselves (LangGraph's get_graph with xray, so nested parts are drawn
whole): the drawing never drifts from what runs."""

import collections
import functools

from langchain_anthropic import ChatAnthropic
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph

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
    """Return a node's plain name from labels.json, or from ``table`` (a {graph: {node: text}} table) when given.

    The node's own graph's entry wins, else any other graph's (an email follow-up runs Traitement's nodes). A start
    or end gets "Début"/"Fin" (or "" from a table); an unknown node gets its id's last part, or ``fallback``."""
    name = node_id.rsplit(":", 1)[-1]
    if name in ENDS:
        return ENDS[name] if table is None else ""
    table = LABELS["steps"] if table is None else table
    for scope in (graph, *table):
        if name in table.get(scope, {}):
            return table[scope][name]
    return name if fallback is None else fallback


def description(graph, node_id):
    """Return what the node does in plain words (labels.json: descriptions.steps), "" when there is none."""
    return label(graph, node_id, LABELS["descriptions"]["steps"], "")


def part_of(node_id):
    """Return the nested part a node is in ("fichier:facture" for "fichier:facture:lecture"); None at the top."""
    return node_id.rsplit(":", 1)[0] if ":" in node_id else None


def drawn_node(graph, node_id, parent, kind):
    """Return a canvas node: {id, label, description, parent, kind}."""
    return {
        "id": node_id,
        "label": label(graph, node_id),
        "description": description(graph, node_id),
        "parent": parent,
        "kind": kind,
    }


def failure_exits(graph, depth, prefix=""):
    """Return the set of (source, target) edges a run takes only when a step failed: the routes routers mark with
    engine.calls.stops_at, with ids as drawn (nested graphs opened ``depth`` deep; True for all, ``prefix`` for
    the enclosing part)."""
    nodes = graph.builder.nodes

    def opened(name):
        return name in nodes and isinstance(nodes[name].runnable, CompiledStateGraph) and depth not in (0, False)

    def drawn(name, side):
        name = "__end__" if name == END else name
        return f"{prefix}{name}:__{side}__" if opened(name) else f"{prefix}{name}"

    found = set()
    for source, branches in graph.builder.branches.items():
        for branch in branches.values():
            target = getattr(getattr(branch.path, "func", None), "stops_at", None)
            if target is not None:
                found.add((drawn(source, "end"), drawn(target, "start")))
    for name, node in nodes.items():
        if opened(name):
            found |= failure_exits(node.runnable, depth if depth is True else depth - 1, f"{prefix}{name}:")
    return found


def hidden(edge, failures, arriving):
    """True when a conditional edge is left off the canvas (a run that took it still shows it).

    Hidden: failure routes (in ``failures``, from failure_exits), which would converge on the summary from every
    step; and routes from a part's start to a node that other edges also reach (``arriving``: node id -> number of
    incoming edges), since a part can resume at any of its steps and only its first entry is drawn."""
    if not edge.conditional:
        return False
    from_start = edge.source.endswith(":__start__") and arriving[edge.target] > 1
    return (edge.source, edge.target) in failures or from_start


@functools.cache
def shape(name):
    """Return the canvas shape of graph ``name`` (a COMPILED key, or "agent"); cached until labels.json changes.

    Returns {graph, label, description, nodes: [{id, label, description, parent, kind}],
    edges: [{id, source, target, conditional, hidden}]}. A nested part's nodes have that part as parent (a group
    node); kind is "end" for a start or an end, "group", or "step"."""
    if name == "agent":
        compiled = agent.make_agent(chat=ChatAnthropic(model="drawing", api_key="unused"))
    else:
        compiled = COMPILED[name]
    depth = XRAY.get(name, True)
    drawn = compiled.get_graph(xray=depth)

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

    failures = failure_exits(compiled, depth)
    arriving = collections.Counter(edge.target for edge in drawn.edges)
    edges = [
        {
            "id": f"{edge.source}->{edge.target}",
            "source": edge.source,
            "target": edge.target,
            "conditional": bool(edge.conditional),
            "hidden": hidden(edge, failures, arriving),
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
