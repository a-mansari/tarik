"""A bank statement's pointing, one step per box, as the cabinet works the statement's « Operations » list: Experio's
own detection, its reading checked against the statement's PDF, then what it leaves (operations, tiers, cited and
open invoices, TPE remittances), then where the statement stands. Traitement runs it on a statement it imported,
Relevés on each of a dossier's open statements.

Each step's result is routine: its ``said`` and ``note`` go into the report's one line (report_statement). A step that
is not ok ends the statement's pointing; the summary says where."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import run_step, stops_at

# Each box and its step, in order.
STEPS = {
    "detection": "detect_statement_lines",
    "lecture": "check_statement_reading",
    "types": "choose_line_operations",
    "tiers": "name_line_tiers",
    "factures_citees": "tie_cited_invoices",
    "factures_ouvertes": "tie_owed_invoices",
    "tpe": "tie_tpe_remittances",
}


class Given(TypedDict, total=False):
    call: dict
    run_id: str
    statement_id: int


class Statement(Given, total=False):
    outcomes: Annotated[list, operator.add]  # this statement's own, the report reads them
    last: dict


class Done(TypedDict, total=False):
    outcomes: Annotated[list, operator.add]


def pointing_step(tool):
    """Return the graph node running pointing step ``tool`` on the statement."""

    async def node(state: Statement):
        words = {"client_message": state["call"].get("client_message") or ""} if tool == "tie_cited_invoices" else {}
        result = await run_step(state, tool, statement_id=state["statement_id"], **words)
        return {"last": result, "outcomes": [result]}

    return node


async def report(state: Statement):
    """Graph node: say where the statement stands, with what the steps did (report_statement)."""
    done = state.get("outcomes") or []
    result = await run_step(
        state,
        "report_statement",
        statement_id=state["statement_id"],
        said=[outcome["said"] for outcome in done if outcome.get("said")],
        notes=[outcome["note"] for outcome in done if outcome.get("note")],
    )
    return {"last": result, "outcomes": [result]}


def ok_then(following):
    """Return (router, routes): on to ``following`` after an ok step, else END (the pointing stops there)."""
    return stops_at(END)(lambda s: following if s["last"].get("status") == "ok" else END), [following, END]


names = list(STEPS)
graph = StateGraph(Statement, input_schema=Given, output_schema=Done)
for index, name in enumerate(names):
    graph.add_node(name, pointing_step(STEPS[name]))
    graph.add_conditional_edges(name, *ok_then(names[index + 1] if index + 1 < len(names) else "bilan"))
graph.add_node("bilan", report)
graph.add_edge(START, names[0])
graph.add_edge("bilan", END)
POINTING = graph.compile(name="pointage")
