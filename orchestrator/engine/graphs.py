"""Each workflow's shape for the UI's canvas: its nodes, nested parts (a file, an invoice) as groups, and its edges,
the conditional ones marked, with the plain words of content/labels.json. Read from the compiled graphs themselves
(LangGraph's get_graph, xray: nested parts drawn whole), so the drawing never drifts from what runs."""

import functools

from engine import agent, dossier, emails, traitement
from engine.content import LABELS

COMPILED = {"traitement": traitement.graph, "dossier": dossier.graph, "emails": emails.graph}
ENDS = {"__start__": "Début", "__end__": "Fin"}


def label(graph, node_id, table=LABELS["steps"], fallback=None):
    """A node's plain name (or, from the descriptions table, what it does): its own graph's words for it, else
    another graph's (an email follow-up runs Traitement)."""
    name = node_id.rsplit(":", 1)[-1]
    if name in ENDS:
        return ENDS[name] if table is LABELS["steps"] else ""
    for scope in (graph, *table):
        if name in table.get(scope, {}):
            return table[scope][name]
    return name if fallback is None else fallback


def description(graph, node_id):
    return label(graph, node_id, LABELS["descriptions"]["steps"], "")


@functools.cache
def shape(name):
    """{nodes: [{id, label, description, parent, kind}], edges: [{id, source, target, conditional}]} for graph ``name``. A nested
    part's nodes have their part as parent (a group); kind is "end" for a start or an end, "group", or "step"."""
    compiled = agent.make_agent() if name == "agent" else COMPILED[name]
    drawn = compiled.get_graph(xray=True)
    nodes, groups = [], {}
    for node_id in drawn.nodes:
        parent = node_id.rsplit(":", 1)[0] if ":" in node_id else None
        prefix = parent
        while prefix:  # every enclosing part becomes a group, its own parent the part around it
            groups.setdefault(prefix, prefix.rsplit(":", 1)[0] if ":" in prefix else None)
            prefix = groups[prefix]
        kind = "end" if node_id.rsplit(":", 1)[-1] in ENDS else "step"
        nodes.append({"id": node_id, "label": label(name, node_id), "description": description(name, node_id), "parent": parent, "kind": kind})
    nodes += [{"id": group, "label": label(name, group), "description": description(name, group), "parent": parent, "kind": "group"} for group, parent in groups.items()]
    leaving = {}
    for edge in drawn.edges:
        leaving[edge.source] = leaving.get(edge.source, 0) + 1
    # A step that is not ok ends its invoice or file: drawn, those paths would converge from every step on the part's
    # end. They are left out where the step has another way on (the legend says it), kept where it is the only one.
    arriving = {}
    for edge in drawn.edges:
        arriving[edge.target] = arriving.get(edge.target, 0) + 1
    # And a part may start at any of its steps (an invoice resumes where an answer leaves it): only its first entry is
    # drawn, the step nothing else leads to; the others would fan out from its start.
    edges = [
        {"id": f"{edge.source}->{edge.target}", "source": edge.source, "target": edge.target, "conditional": bool(edge.conditional)}
        for edge in drawn.edges
        if not (edge.conditional and edge.target.endswith(":__end__") and leaving[edge.source] > 1)
        and not (edge.conditional and edge.source.endswith(":__start__") and arriving[edge.target] > 1)
    ]
    return {"graph": name, "label": LABELS["graphs"].get(name, name), "description": LABELS["descriptions"]["graphs"].get(name, ""), "nodes": nodes, "edges": edges}
