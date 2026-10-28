"""The Relevés graph: point a dossier's bank statements already in Experio and not posted yet (imported by the
cabinet, or by Tarik earlier), each through the same pointing steps as a statement Traitement imports
(engine.pointage), one statement at a time, and post them when the client asks. The sender must have access to the
dossier."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from engine.calls import ok_or_summary, run_step, sender_checked, started, stops_at, summary
from engine.pointage import POINTING

INPUTS = ["session_id", "dossier_id", "comptabiliser", "client_message", "attachments", "cabinet"]


class Run(TypedDict, total=False):
    call: dict
    run_id: str
    outcomes: Annotated[list, operator.add]
    statement_ids: list  # the dossier's open statements (list_open_statements)
    message: str


async def sender(state: Run):
    """Graph node: check the sender may work on the call's dossier (check_sender)."""
    return await sender_checked(state, state["call"]["dossier_id"])


async def statements(state: Run):
    """Graph node: list the dossier's open statements (list_open_statements)."""
    result = await run_step(state, "list_open_statements", dossier_id=state["call"]["dossier_id"])
    return {"statement_ids": result.get("statement_ids") or [], "outcomes": [result]}


@stops_at("resume")
def each_statement(state: Run):
    """Route: one pointing run per open statement; "resume" when the listing failed or found none."""
    if state["outcomes"][-1].get("status") != "ok" or not state.get("statement_ids"):
        return "resume"
    given = {"call": state["call"], "run_id": state["run_id"]}
    return [Send("pointage", {**given, "statement_id": i}) for i in state["statement_ids"]]


async def post(state: Run):
    """Graph node: post the dossier's pointed statements (post_dossier_statements)."""
    result = await run_step(state, "post_dossier_statements", dossier_id=state["call"]["dossier_id"])
    return {"outcomes": [result]}


def after_pointing(state: Run):
    """Route to "comptabilisation" when the client asked to post (comptabiliser "oui"), else "resume"."""
    return "comptabilisation" if state["call"].get("comptabiliser") == "oui" else "resume"


run = StateGraph(Run)
run.add_node("debut", started(INPUTS))
run.add_node("expediteur", sender)
run.add_node("releves", statements)
run.add_node("pointage", POINTING)
run.add_node("comptabilisation", post)
run.add_node("resume", summary)
run.add_edge(START, "debut")
run.add_conditional_edges("debut", *ok_or_summary("expediteur"))  # a failed save goes no further
run.add_conditional_edges("expediteur", *ok_or_summary("releves"))
run.add_conditional_edges("releves", each_statement, ["pointage", "resume"])
run.add_conditional_edges("pointage", after_pointing, ["comptabilisation", "resume"])
run.add_edge("comptabilisation", "resume")
run.add_edge("resume", END)
# One statement at a time: two statements of one dossier never race on the same invoice.
graph = run.compile(name="Tarik - Relevés").with_config(max_concurrency=1)
