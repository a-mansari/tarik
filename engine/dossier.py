"""Dossier as a graph: a dossier's creation (or a spreadsheet's list of them), by the cabinet's managers and Experio's
staff only, then its configuration and entries import; or (mode update, person, archive) one change to a dossier
assigned to the sender; mode resume finishes the dossiers left waiting. No time budget: finish_setup gets
seconds_left 0 (no limit)."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.tools import sender_checked, started, step, summary

INPUTS = [
    "mode",
    "session_id",
    "dossier_id",
    "name",
    "ice",
    "if_number",
    "folder_type",
    "skip_ice_if_check",
    "client_confirmed",
    "client_message",
    "model_dossier_id",
    "kind",
    "finish",
    "person_kind",
    "person_name",
    "email",
    "phone",
    "profile",
    "rc",
    "patente",
    "tsc",
    "capital",
    "cnss",
    "street",
    "street2",
    "city",
    "zip",
    "attachments",
    "cabinet",
]
IDENTITY = [
    "name",
    "folder_type",
    "ice",
    "if_number",
    "rc",
    "patente",
    "tsc",
    "capital",
    "cnss",
    "street",
    "street2",
    "city",
    "zip",
]


class Run(TypedDict, total=False):
    call: dict
    run_id: str
    target: int  # the dossier the rest works on: the one given, or the one just created
    outcomes: Annotated[list, operator.add]
    message: str


def ids(call):
    return call["cabinet"], call["session_id"]


async def start(state: Run, config):
    begun = await started(INPUTS)(state, config)
    return {**begun, "target": begun["call"].get("dossier_id") or 0}


def by_mode(state: Run):
    call = state["call"]
    if state["outcomes"][-1].get("status") != "ok":
        return "resume"  # a failed save is recorded and goes no further: the files the run would work on are missing
    mode = call.get("mode")
    if mode in ("update", "person", "archive"):
        return "expediteur"
    if mode == "resume":
        return "reprise"
    if mode != "dossiers" and (call.get("dossier_id") or 0) > 0:
        return "expediteur"
    return "createur"  # a dossier, or a spreadsheet's list of them, to create


async def create(state: Run):
    call = state["call"]
    cabinet, session = ids(call)
    result = await step(
        cabinet,
        state["run_id"],
        session,
        "create_dossier",
        name=call.get("name") or "",
        ice=call.get("ice") or "",
        if_number=call.get("if_number") or "",
        folder_type=call.get("folder_type") or "company",
        skip_ice_if_check=call.get("skip_ice_if_check") is True,
        client_confirmed=call.get("client_confirmed") is True,
        client_message=call.get("client_message") or "",
        session_id=session,
    )
    update = {"outcomes": [result]}
    if result.get("status") == "ok":
        update["target"] = result["dossier_id"]
        # A dossier reused from earlier in the conversation keeps its configuration.
        if (call.get("model_dossier_id") or 0) > 0 and not result.get("reused"):
            copied = await step(
                cabinet,
                state["run_id"],
                session,
                "copy_configuration",
                dossier_id=result["dossier_id"],
                model_dossier_id=call["model_dossier_id"],
            )
            update["outcomes"].append(copied)
    return update


async def creator(state: Run):
    """Only the cabinet's managers and Experio's staff create dossiers (check_creator)."""
    given = state["call"]
    result = await step(
        given["cabinet"], state["run_id"], given["session_id"], "check_creator", session_id=given["session_id"]
    )
    return {"outcomes": [result]}


def after_creator(state: Run):
    if state["outcomes"][-1].get("status") != "ok":
        return "resume"
    return "listes" if state["call"].get("mode") == "dossiers" else "creation"


async def sender(state: Run):
    return await sender_checked(state, state["call"]["dossier_id"])


def after_sender(state: Run):
    if state["outcomes"][-1].get("status") != "ok":
        return "resume"
    return {"update": "modification", "person": "acces", "archive": "archivage"}.get(
        state["call"].get("mode"), "ecritures"
    )


async def update(state: Run):
    call = state["call"]
    result = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "update_dossier",
        dossier_id=call["dossier_id"],
        **{field: call.get(field) or "" for field in IDENTITY},
        skip_ice_if_check=call.get("skip_ice_if_check") is True,
        client_confirmed=call.get("client_confirmed") is True,
        client_message=call.get("client_message") or "",
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


async def person(state: Run):
    call = state["call"]
    result = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "create_person",
        kind=call.get("person_kind") or "",
        dossier_id=call["dossier_id"],
        name=call.get("person_name") or "",
        email=call.get("email") or "",
        phone=call.get("phone") or "",
        profile=call.get("profile") or "",
        client_confirmed=call.get("client_confirmed") is True,
        client_message=call.get("client_message") or "",
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


async def archive(state: Run):
    call = state["call"]
    result = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "archive_dossier",
        dossier_id=call["dossier_id"],
        client_confirmed=call.get("client_confirmed") is True,
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


async def lists(state: Run):
    """Mode dossiers: each spreadsheet of the message, its dossiers created once the client confirmed the list."""
    call = state["call"]
    cabinet, session = ids(call)
    found = await step(cabinet, state["run_id"], session, "files_to_process", session_id=session, entries=True)
    outcomes = [found]
    for file_id in found.get("files", []):
        outcomes.append(
            await step(
                cabinet,
                state["run_id"],
                session,
                "import_dossiers",
                file_id=file_id,
                client_confirmed=call.get("client_confirmed") is True,
                session_id=session,
            )
        )
    return {"outcomes": outcomes}


async def entries(state: Run):
    """Each file of the message imported, then the onboarding, unless an import was refused: the bank of each
    bank journal, the integration, the dossier's state."""
    call = state["call"]
    cabinet, session = ids(call)
    target = state["target"]
    found = await step(cabinet, state["run_id"], session, "files_to_process", session_id=session, entries=True)
    outcomes = [found]
    for file_id in found.get("files", []):
        outcomes.append(
            await step(
                cabinet,
                state["run_id"],
                session,
                "import_entries",
                dossier_id=target,
                file_id=file_id,
                kind=call.get("kind") or "",
            )
        )
    if any(result.get("status") != "ok" for result in outcomes[1:]):
        return {"outcomes": outcomes}  # a refused file: the client sends it again before anything is finished
    outcomes.append(await step(cabinet, state["run_id"], session, "ask_banks", dossier_id=target))
    finished = await step(
        cabinet,
        state["run_id"],
        session,
        "finish_setup",
        dossier_id=target,
        seconds_left=0,
        finish=call.get("finish") is True,
    )
    outcomes.append(finished)
    if finished.get("status") == "ok":
        outcomes.append(await step(cabinet, state["run_id"], session, "setup_status", dossier_id=target))
    return {"outcomes": outcomes}


async def resume_setups(state: Run):
    """Mode resume: the conversation's dossiers left waiting, each finished then its state."""
    call = state["call"]
    cabinet, session = ids(call)
    found = await step(cabinet, state["run_id"], session, "waiting_setups", session_id=session)
    outcomes = [found]
    for dossier_id in found.get("dossiers", []):
        finished = await step(cabinet, state["run_id"], session, "finish_setup", dossier_id=dossier_id)
        outcomes.append(finished)
        if finished.get("status") == "ok":
            outcomes.append(await step(cabinet, state["run_id"], session, "setup_status", dossier_id=dossier_id))
    return {"outcomes": outcomes}


run = StateGraph(Run)
for name, node in {
    "debut": start,
    "createur": creator,
    "creation": create,
    "expediteur": sender,
    "modification": update,
    "acces": person,
    "archivage": archive,
    "listes": lists,
    "ecritures": entries,
    "reprise": resume_setups,
    "resume": summary,
}.items():
    run.add_node(name, node)
run.add_edge(START, "debut")
run.add_conditional_edges("debut", by_mode, ["createur", "expediteur", "reprise", "resume"])
run.add_conditional_edges("createur", after_creator, ["creation", "listes", "resume"])
run.add_conditional_edges(
    "creation", lambda s: "ecritures" if s["outcomes"][-1].get("status") == "ok" else "resume", ["ecritures", "resume"]
)
run.add_conditional_edges("expediteur", after_sender, ["modification", "acces", "archivage", "ecritures", "resume"])
for name in ("modification", "acces", "archivage", "listes", "ecritures", "reprise"):
    run.add_edge(name, "resume")
run.add_edge("resume", END)
graph = run.compile(name="Tarik - Dossier")
