"""Déclarations as a graph: one of a dossier's declarations prepared in Experio, declared only on the client's words
and confirmation (mode tva: the TVA declaration; ras: the withholding declarations; delais: the payment delays,
loi 69-21), for a sender who has the dossier."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import failed, ok_or_summary, run_step, sender_checked, started, summary

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
    return await sender_checked(state, state["call"]["dossier_id"])


def by_mode(state: Run):
    if failed(state):
        return "resume"
    return NODES.get(state["call"].get("mode"), "resume")


def asked(call):
    """What every declaration takes from the client's words."""
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
    return {"outcomes": [await run_step(state, "prepare_tva_declaration", credit_reporte=credit, **asked(call))]}


async def ras(state: Run):
    call = state["call"]
    kinds = call.get("declarations") or ""
    return {"outcomes": [await run_step(state, "prepare_ras_declaration", declarations=kinds, **asked(call))]}


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
