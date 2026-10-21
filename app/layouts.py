"""How the team arranged each workflow's canvas: where each node sits, and the bends and style of each edge. One
layout per workflow, shared by everyone, kept in Postgres (table layouts); a node without a saved place keeps the
automatic one."""

from psycopg.types.json import Jsonb

from engine import runs
from tarik.core.queries import Q

STYLES = {"straight", "taxi", "bezier"}  # straight (the default), right angles, curved
LIMIT = 2000  # nodes or edges in one layout: far over any workflow's


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e6:
        raise ValueError("a position is a number")
    return round(float(value), 1)


def cleaned(layout):
    """``layout`` as it may be kept: positions as numbers, known edge styles, nothing else. ValueError otherwise."""
    nodes, edges = layout.get("nodes") or {}, layout.get("edges") or {}
    if not isinstance(nodes, dict) or not isinstance(edges, dict) or len(nodes) > LIMIT or len(edges) > LIMIT:
        raise ValueError("nodes and edges are objects of a reasonable size")
    kept_nodes = {str(key): {"x": number(p["x"]), "y": number(p["y"])} for key, p in nodes.items()}
    kept_edges = {}
    for key, edge in edges.items():
        points = [{"x": number(p["x"]), "y": number(p["y"])} for p in (edge.get("points") or [])][:20]
        style = edge.get("style") if edge.get("style") in STYLES else "straight"
        kept_edges[str(key)] = {"points": points, "style": style}
    return {"nodes": kept_nodes, "edges": kept_edges}


async def read(graph):
    async with runs.pool.connection() as db:
        found = await (await db.execute(Q.layout_read, (graph,))).fetchone()
    return found[0] if found else {"nodes": {}, "edges": {}}


async def save(graph, layout):
    kept = cleaned(layout)
    async with runs.pool.connection() as db:
        await db.execute(
            Q.layout_saved,
            (graph, Jsonb(kept)),
        )
    return kept


async def forget(graph):
    async with runs.pool.connection() as db:
        await db.execute(Q.layout_forgotten, (graph,))
