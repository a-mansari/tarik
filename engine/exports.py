"""Export as a graph: a dossier's entries by journal, its tiers or its FEC exported with Experio's own export, its file
sent with Tarik's reply, for a sender who has the dossier."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import ok_or_summary, run_step, sender_checked, started, summary

INPUTS = [
    "session_id",
    "dossier_id",
    "quoi",
    "periode",
    "modele",
    "format_fichier",
    "lettrage",
    "integration",
    "date_import",
    "client_confirmed",
    "client_message",
    "attachments",
    "cabinet",
]


class Run(TypedDict, total=False):
    call: dict
    run_id: str
    outcomes: Annotated[list, operator.add]
    message: str


async def sender(state: Run):
    return await sender_checked(state, state["call"]["dossier_id"])


async def export(state: Run):
    call = state["call"]
    result = await run_step(
        state,
        "export_entries",
        dossier_id=call["dossier_id"],
        quoi=call.get("quoi") or "",
        periode=call.get("periode") or "",
        modele=call.get("modele") or "",
        format_fichier=call.get("format_fichier") or "",
        lettrage=call.get("lettrage") is True,
        integration=call.get("integration") is True,
        date_import=call.get("date_import") is True,
        client_confirmed=call.get("client_confirmed") is True,
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


run = StateGraph(Run)
run.add_node("debut", started(INPUTS))
run.add_node("expediteur", sender)
run.add_node("export", export)
run.add_node("resume", summary)
run.add_edge(START, "debut")
run.add_conditional_edges("debut", *ok_or_summary("expediteur"))  # a failed save goes no further
run.add_conditional_edges("expediteur", *ok_or_summary("export"))
run.add_edge("export", "resume")
run.add_edge("resume", END)
graph = run.compile(name="Tarik - Export")
