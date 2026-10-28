"""The Relevés graph: point a dossier's bank statements already in Experio and not posted yet (imported by the
cabinet, or by Tarik earlier), as Traitement points one it imports, and post them when the client asks. The sender
must have access to the dossier."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import ok_or_summary, run_step, sender_checked, started, stops_at, summary

INPUTS = ["session_id", "dossier_id", "comptabiliser", "client_message", "attachments", "cabinet"]


class Run(TypedDict, total=False):
    call: dict
    run_id: str
    outcomes: Annotated[list, operator.add]
    message: str


async def sender(state: Run):
    """Graph node: check the sender may work on the call's dossier (check_sender)."""
    return await sender_checked(state, state["call"]["dossier_id"])


async def point(state: Run):
    """Graph node: point the dossier's open statements (settle_dossier_statements)."""
    call = state["call"]
    result = await run_step(
        state,
        "settle_dossier_statements",
        dossier_id=call["dossier_id"],
        client_message=call.get("client_message") or "",
    )
    return {"outcomes": [result]}


async def post(state: Run):
    """Graph node: post the dossier's pointed statements (post_dossier_statements)."""
    result = await run_step(state, "post_dossier_statements", dossier_id=state["call"]["dossier_id"])
    return {"outcomes": [result]}


@stops_at("resume")
def after_pointing(state: Run):
    """Route to "comptabilisation" only after an ok pointing when the client asked to post (comptabiliser "oui"),
    else "resume"."""
    asked = state["call"].get("comptabiliser") == "oui"
    last = state["outcomes"][-1]
    return "comptabilisation" if asked and last.get("status") == "ok" and not last.get("routine") else "resume"


run = StateGraph(Run)
run.add_node("debut", started(INPUTS))
run.add_node("expediteur", sender)
run.add_node("pointage", point)
run.add_node("comptabilisation", post)
run.add_node("resume", summary)
run.add_edge(START, "debut")
run.add_conditional_edges("debut", *ok_or_summary("expediteur"))  # a failed save goes no further
run.add_conditional_edges("expediteur", *ok_or_summary("pointage"))
run.add_conditional_edges("pointage", after_pointing, ["comptabilisation", "resume"])
run.add_edge("comptabilisation", "resume")
run.add_edge("resume", END)
graph = run.compile(name="Tarik - Relevés")
