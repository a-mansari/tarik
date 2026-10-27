"""The Déclarations graph: prepare one of a dossier's declarations in Experio, and declare it only when the client
asked for it and confirmed.

Modes: tva (the TVA declaration), ras (the withholding declarations), delais (the payment delays declaration, law
69-21, Experio's exp.payment.delay). The sender must have access to the dossier. Input routine: a routine's
unattended run, which makes a declaration but never takes one up (new_only)."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import failed, ok_or_summary, run_step, sender_checked, started, stops_at, summary

INPUTS = [
    "mode",
    "session_id",
    "dossier_id",
    "periode",
    "credit_reporte",
    "declarations",
    "chiffre_affaires",
    "declarer",
    "client_confirmed",
    "client_message",
    "routine",
    "attachments",
    "cabinet",
]
NODES = {"tva": "tva", "ras": "ras", "delais": "delais"}  # mode: its node


class Run(TypedDict, total=False):
    call: dict
    run_id: str
    outcomes: Annotated[list, operator.add]
    message: str


async def sender(state: Run):
    """Graph node: check the sender may work on the call's dossier (check_sender)."""
    return await sender_checked(state, state["call"]["dossier_id"])


@stops_at("resume")
def by_mode(state: Run):
    """Route to the call's mode node (tva, ras, delais); "resume" when the last step failed or the mode is unknown."""
    if failed(state):
        return "resume"
    return NODES.get(state["call"].get("mode"), "resume")


def asked(call):
    """Return the arguments every declaration tool takes from the call: dossier_id, periode, declarer,
    client_confirmed (both True only when given exactly True) and session_id."""
    return {
        "dossier_id": call["dossier_id"],
        "periode": call.get("periode") or "",
        "declarer": call.get("declarer") is True,
        "client_confirmed": call.get("client_confirmed") is True,
        "session_id": call["session_id"],
    }


async def tva(state: Run):
    call = state["call"]
    credit = str(call.get("credit_reporte") or "")
    new_only = call.get("routine") is True  # a routine's run: an existing declaration is never taken up
    result = await run_step(state, "prepare_tva_declaration", credit_reporte=credit, new_only=new_only, **asked(call))
    return {"outcomes": [result]}


async def ras(state: Run):
    call = state["call"]
    kinds = call.get("declarations") or ""
    new_only = call.get("routine") is True
    result = await run_step(state, "prepare_ras_declaration", declarations=kinds, new_only=new_only, **asked(call))
    return {"outcomes": [result]}


async def delays(state: Run):
    call = state["call"]
    turnover = str(call.get("chiffre_affaires") or "")
    result = await run_step(state, "prepare_payment_delay_declaration", chiffre_affaires=turnover, **asked(call))
    return {"outcomes": [result]}


run = StateGraph(Run)
for name, node in {
    "debut": started(INPUTS),
    "expediteur": sender,
    "tva": tva,
    "ras": ras,
    "delais": delays,
    "resume": summary,
}.items():
    run.add_node(name, node)
run.add_edge(START, "debut")
run.add_conditional_edges("debut", *ok_or_summary("expediteur"))  # a failed save goes no further
run.add_conditional_edges("expediteur", by_mode, [*NODES.values(), "resume"])
for name in NODES.values():
    run.add_edge(name, "resume")
run.add_edge("resume", END)
graph = run.compile(name="Tarik - Déclarations")
