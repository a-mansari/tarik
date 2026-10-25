"""Dossier as a graph: a dossier's creation (or a spreadsheet's list of them), by the cabinet's managers and Experio's
staff only, then its configuration copied, its files' entries imported, the bank of its bank journals asked, its
onboarding finished and its state said; or (mode update, person, archive, difference) one change to a dossier assigned
to the sender (difference: a posted invoice's remainder written off); mode resume finishes the dossiers left waiting.
One step per node, so the canvas shows each. No time budget: finish_setup gets seconds_left 0 (no limit)."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import failed, run_step, sender_checked, started, summary
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
    files: list  # the message's files to import (entries, or a spreadsheet of dossiers)
    finishing: list  # the dossiers whose onboarding is finished next
    finish_options: dict  # how finish_setup finishes them (seconds_left, finish)
    finished: list  # those finished: their state is said


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
    return update


def after_create(state: Run):
    """A new dossier takes the model dossier's configuration first (a dossier reused from earlier in the conversation
    keeps its own), then its files."""
    if failed(state):
        return "resume"
    last = state["outcomes"][-1]
    if (state["call"].get("model_dossier_id") or 0) > 0 and not last.get("reused"):
        return "configuration"
    return "fichiers"


async def configuration(state: Run):
    copied = await run_step(
        state, "copy_configuration", dossier_id=state["target"], model_dossier_id=state["call"]["model_dossier_id"]
    )
    return {"outcomes": [copied]}


async def creator(state: Run):
    """Only the cabinet's managers and Experio's staff create dossiers (check_creator)."""
    return {"outcomes": [await run_step(state, "check_creator", session_id=state["call"]["session_id"])]}


def after_creator(state: Run):
    if failed(state):
        return "resume"
    return "fichiers" if state["call"].get("mode") == "dossiers" else "creation"


async def sender(state: Run):
    return await sender_checked(state, state["call"]["dossier_id"])


def after_sender(state: Run):
    if failed(state):
        return "resume"
    return {
        "update": "modification",
        "person": "acces",
        "archive": "archivage",
        "difference": "ecart",
    }.get(state["call"].get("mode"), "fichiers")


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


async def files(state: Run):
    """The message's files to import: a spreadsheet of dossiers (mode dossiers), else a dossier's entries."""
    found = await run_step(state, "files_to_process", session_id=state["call"]["session_id"], entries=True)
    return {"files": found.get("files", []), "outcomes": [found]}


def after_files(state: Run):
    if failed(state):
        return "resume"
    return "listes" if state["call"].get("mode") == "dossiers" else "ecritures"


async def lists(state: Run):
    """Mode dossiers: each spreadsheet of the message, its dossiers created once the client confirmed the list."""
    call = state["call"]
    outcomes = [
        await run_step(
            state,
            "import_dossiers",
            file_id=file_id,
            client_confirmed=call.get("client_confirmed") is True,
            session_id=call["session_id"],
        )
        for file_id in state.get("files", [])
    ]
    return {"outcomes": outcomes}


async def entries(state: Run):
    """Each file of the message imported into the dossier."""
    call = state["call"]
    outcomes = [
        await run_step(
            state, "import_entries", dossier_id=state["target"], file_id=file_id, kind=call.get("kind") or ""
        )
        for file_id in state.get("files", [])
    ]
    return {"outcomes": outcomes}


def after_entries(state: Run):
    """The onboarding goes on unless an import was refused (the client sends that file again first)."""
    imported = state["outcomes"][-len(state["files"]) :] if state.get("files") else []
    if any(result.get("status") != "ok" for result in imported):
        return "resume"
    return "banques"


async def banks(state: Run):
    """The bank of each bank journal of the dossier, then its onboarding finished (no time limit)."""
    asked = await run_step(state, "ask_banks", dossier_id=state["target"])
    options = {"seconds_left": 0, "finish": state["call"].get("finish") is True}
    return {"outcomes": [asked], "finishing": [state["target"]], "finish_options": options}


async def resume_setups(state: Run):
    """Mode resume: the conversation's dossiers left waiting, to finish."""
    found = await run_step(state, "waiting_setups", session_id=state["call"]["session_id"])
    return {"outcomes": [found], "finishing": found.get("dossiers", []), "finish_options": {}}


async def finish(state: Run):
    """Each dossier's onboarding finished (finish_setup): those it finished have their state said next."""
    outcomes, done = [], []
    for dossier_id in state.get("finishing", []):
        result = await run_step(state, "finish_setup", dossier_id=dossier_id, **state.get("finish_options", {}))
        outcomes.append(result)
        if result.get("status") == "ok":
            done.append(dossier_id)
    return {"outcomes": outcomes, "finished": done}


def after_finish(state: Run):
    return "etat" if state.get("finished") else "resume"


async def status(state: Run):
    """Each finished dossier's state (setup_status)."""
    outcomes = [await run_step(state, "setup_status", dossier_id=d) for d in state.get("finished", [])]
    return {"outcomes": outcomes}


run = StateGraph(Run)
for name, node in {
    "debut": start,
    "createur": creator,
    "creation": create,
    "configuration": configuration,
    "expediteur": sender,
    "modification": update,
    "acces": person,
    "archivage": archive,
    "ecart": difference,
    "fichiers": files,
    "listes": lists,
    "ecritures": entries,
    "banques": banks,
    "reprise": resume_setups,
    "finalisation": finish,
    "etat": status,
    "resume": summary,
}.items():
    run.add_node(name, node)
run.add_edge(START, "debut")
run.add_conditional_edges("debut", by_mode, ["createur", "expediteur", "reprise", "resume"])
run.add_conditional_edges("createur", after_creator, ["creation", "fichiers", "resume"])
run.add_conditional_edges("creation", after_create, ["configuration", "fichiers", "resume"])
run.add_edge("configuration", "fichiers")
run.add_conditional_edges(
    "expediteur", after_sender, ["modification", "acces", "archivage", "ecart", "fichiers", "resume"]
)
run.add_conditional_edges("fichiers", after_files, ["listes", "ecritures", "resume"])
run.add_conditional_edges("ecritures", after_entries, ["banques", "resume"])
run.add_edge("banques", "finalisation")
run.add_edge("reprise", "finalisation")
run.add_conditional_edges("finalisation", after_finish, ["etat", "resume"])
for name in ("modification", "acces", "archivage", "ecart", "listes", "etat"):
    run.add_edge(name, "resume")
run.add_edge("resume", END)
graph = run.compile(name="Tarik - Dossier")
