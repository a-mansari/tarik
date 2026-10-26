"""Store how the team arranged each workflow's canvas: where each node sits, and the bends and style of each edge.

One layout per workflow, shared by everyone, kept in Postgres (table layouts). A node without a saved position keeps
the automatic one."""

from psycopg.types.json import Jsonb

from engine import runs
from engine.tools.core.queries import Q

STYLES = {"straight", "taxi", "bezier"}  # straight (the default), right angles, curved
LIMIT = 2000  # nodes or edges in one layout: far over any workflow's


def number(value):
    """Return ``value`` as a float rounded to 0.1; ValueError unless it is a number (not a bool) within ±1e6."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e6:
        raise ValueError("a position is a number")
    return round(float(value), 1)


def cleaned(layout):
    """Return ``layout`` reduced to what may be stored: node positions as numbers, edges with at most 20 bend points and
    a known style.

    Raises ValueError when nodes or edges are not objects, hold more than LIMIT entries, or a position is not a
    number."""
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
    """Return workflow ``graph``'s saved layout ({nodes, edges}), or an empty one when none is saved."""
    async with runs.pool.connection() as db:
        found = await (await db.execute(Q.layout_read, (graph,))).fetchone()
    return found[0] if found else {"nodes": {}, "edges": {}}


async def save(graph, layout):
    """Clean ``layout`` (see cleaned), store it as workflow ``graph``'s layout, and return what was stored."""
    kept = cleaned(layout)
    async with runs.pool.connection() as db:
        await db.execute(
            Q.layout_saved,
            (graph, Jsonb(kept)),
        )
    return kept


async def forget(graph):
    """Delete workflow ``graph``'s saved layout, so every node goes back to its automatic position."""
    async with runs.pool.connection() as db:
        await db.execute(Q.layout_forgotten, (graph,))
