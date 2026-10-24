"""Dossier as a graph: a dossier's creation (or a spreadsheet's list of them), by the cabinet's managers and Experio's
staff only, then its configuration and entries import; or (mode update, person, archive, difference) one change to
a dossier assigned to the sender (difference: a posted invoice's remainder written off); mode resume finishes the
dossiers left waiting. No time budget: finish_setup gets seconds_left 0 (no limit)."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import failed, ok_or_summary, run_step, sender_checked, started, summary
from engine.tools.dossiers.identity import EDITABLE

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
    "facture",
    "attachments",
    "cabinet",
]
IDENTITY = list(EDITABLE)  # what update_dossier may change, as it names them


class Run(TypedDict, total=False):
    call: dict
    run_id: str
    target: int  # the dossier the rest works on: the one given, or the one just created
    outcomes: Annotated[list, operator.add]
    message: str


async def start(state: Run, config):
    begun = await started(INPUTS)(state, config)
    return {**begun, "target": begun["call"].get("dossier_id") or 0}


def by_mode(state: Run):
    call = state["call"]
    if failed(state):
        return "resume"  # a failed save is recorded and goes no further: the files the run would work on are missing
    mode = call.get("mode")
    if mode in ("update", "person", "archive", "difference"):
        return "expediteur"
    if mode == "resume":
        return "reprise"
    if mode != "dossiers" and (call.get("dossier_id") or 0) > 0:
        return "expediteur"
    return "createur"  # a dossier, or a spreadsheet's list of them, to create


async def create(state: Run):
    call = state["call"]
    result = await run_step(
        state,
        "create_dossier",
        name=call.get("name") or "",
        ice=call.get("ice") or "",
        if_number=call.get("if_number") or "",
        folder_type=call.get("folder_type") or "company",
        skip_ice_if_check=call.get("skip_ice_if_check") is True,
        client_confirmed=call.get("client_confirmed") is True,
        client_message=call.get("client_message") or "",
        session_id=call["session_id"],
    )
    update = {"outcomes": [result]}
    if result.get("status") == "ok":
        update["target"] = result["dossier_id"]
        # A dossier reused from earlier in the conversation keeps its configuration.
        if (call.get("model_dossier_id") or 0) > 0 and not result.get("reused"):
            copied = await run_step(
                state,
                "copy_configuration",
                dossier_id=result["dossier_id"],
                model_dossier_id=call["model_dossier_id"],
            )
            update["outcomes"].append(copied)
    return update


async def creator(state: Run):
    """Only the cabinet's managers and Experio's staff create dossiers (check_creator)."""
    return {"outcomes": [await run_step(state, "check_creator", session_id=state["call"]["session_id"])]}


def after_creator(state: Run):
    if failed(state):
        return "resume"
    return "listes" if state["call"].get("mode") == "dossiers" else "creation"


async def sender(state: Run):
    return await sender_checked(state, state["call"]["dossier_id"])


def after_sender(state: Run):
    if failed(state):
        return "resume"
    return {"update": "modification", "person": "acces", "archive": "archivage", "difference": "ecart"}.get(
        state["call"].get("mode"), "ecritures"
    )


async def update(state: Run):
    call = state["call"]
    result = await run_step(
        state,
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
    result = await run_step(
        state,
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
    result = await run_step(
        state,
        "archive_dossier",
        dossier_id=call["dossier_id"],
        client_confirmed=call.get("client_confirmed") is True,
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


async def difference(state: Run):
    call = state["call"]
    result = await run_step(
        state,
        "settle_difference",
        dossier_id=call["dossier_id"],
        facture=call.get("facture") or "",
        client_confirmed=call.get("client_confirmed") is True,
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


async def lists(state: Run):
    """Mode dossiers: each spreadsheet of the message, its dossiers created once the client confirmed the list."""
    call = state["call"]
    session = call["session_id"]
    found = await run_step(state, "files_to_process", session_id=session, entries=True)
    outcomes = [found]
    for file_id in found.get("files", []):
        outcomes.append(
            await run_step(
                state,
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
    target = state["target"]
    found = await run_step(state, "files_to_process", session_id=call["session_id"], entries=True)
    outcomes = [found]
    for file_id in found.get("files", []):
        outcomes.append(
            await run_step(
                state,
                "import_entries",
                dossier_id=target,
                file_id=file_id,
                kind=call.get("kind") or "",
            )
        )
    if any(result.get("status") != "ok" for result in outcomes[1:]):
        return {"outcomes": outcomes}  # a refused file: the client sends it again before anything is finished
    outcomes.append(await run_step(state, "ask_banks", dossier_id=target))
    outcomes += await finished(state, target, seconds_left=0, finish=call.get("finish") is True)
    return {"outcomes": outcomes}


async def finished(state, dossier_id, **arguments):
    """The dossier's onboarding finished (finish_setup, given ``arguments``), then, once it is, its state
    (setup_status): their outcomes."""
    done = await run_step(state, "finish_setup", dossier_id=dossier_id, **arguments)
    if done.get("status") != "ok":
        return [done]
    return [done, await run_step(state, "setup_status", dossier_id=dossier_id)]


async def resume_setups(state: Run):
    """Mode resume: the conversation's dossiers left waiting, each finished then its state."""
    found = await run_step(state, "waiting_setups", session_id=state["call"]["session_id"])
    outcomes = [found]
    for dossier_id in found.get("dossiers", []):
        outcomes += await finished(state, dossier_id)
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
    "ecart": difference,
    "listes": lists,
    "ecritures": entries,
    "reprise": resume_setups,
    "resume": summary,
}.items():
    run.add_node(name, node)
run.add_edge(START, "debut")
run.add_conditional_edges("debut", by_mode, ["createur", "expediteur", "reprise", "resume"])
run.add_conditional_edges("createur", after_creator, ["creation", "listes", "resume"])
run.add_conditional_edges("creation", *ok_or_summary("ecritures"))
run.add_conditional_edges(
    "expediteur", after_sender, ["modification", "acces", "archivage", "ecart", "ecritures", "resume"]
)
for name in ("modification", "acces", "archivage", "ecart", "listes", "ecritures", "reprise"):
    run.add_edge(name, "resume")
run.add_edge("resume", END)
graph = run.compile(name="Tarik - Dossier")
