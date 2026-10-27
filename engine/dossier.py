"""The Dossier graph: create and set up dossiers, or make one change to a dossier.

- Creation (the cabinet's managers and Experio's staff only, check_creator): create the dossier, or each dossier of a
  spreadsheet (mode dossiers); copy a model dossier's configuration; import the files' entries; ask the bank of each
  bank journal; finish the onboarding and report its state.
- Modes update, archive, difference: one change to a dossier the sender has (difference: write off a posted
  invoice's remainder).
- Modes person, access: create and invite a person, or change one's access (a dossier given or taken, a role, an
  invitation sent again), for a sender whose own Experio profile may manage users (check_manager).
- Mode routine: make, list, pause, resume or delete a routine (manage_routine), after the same check.
- Mode resume: finish the conversation's dossiers left waiting.
One step per node, so the canvas shows each. No time budget: finish_setup gets seconds_left 0 (no limit)."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import failed, ok_or_summary, run_step, sender_checked, started, stops_at, summary
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
    "action",
    "routine_kind",
    "every",
    "days",
    "hour",
    "routine_id",
    "prompt",
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
    """Graph node: the common start (engine.calls.started), plus "target" set to the call's dossier_id (0 if none)."""
    begun = await started(INPUTS)(state, config)
    return {**begun, "target": begun["call"].get("dossier_id") or 0}


@stops_at("resume")
def by_mode(state: Run):
    """Route after the start: "resume" on a failed save; "gestionnaire" for a person or an access change;
    "expediteur" for another change mode or a given dossier;
    "reprise" for mode resume; else "createur" (create a dossier or a spreadsheet's dossiers)."""
    call = state["call"]
    if failed(state):
        return "resume"  # a failed save is recorded and goes no further: the files the run would work on are missing
    mode = call.get("mode")
    if mode in ("person", "access", "routine"):
        return "gestionnaire"
    if mode in ("update", "archive", "difference"):
        return "expediteur"
    if mode == "resume":
        return "reprise"
    if mode != "dossiers" and (call.get("dossier_id") or 0) > 0:
        return "expediteur"
    return "createur"  # a dossier, or a spreadsheet's list of them, to create


async def create(state: Run):
    """Graph node: create the dossier (create_dossier); on an ok result "target" becomes the new dossier's id."""
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


@stops_at("resume")
def after_create(state: Run):
    """Route after creation: "configuration" when a model dossier was given and the dossier is new (not reused from
    earlier in the conversation), else "fichiers"; "resume" when creation failed."""
    if failed(state):
        return "resume"
    last = state["outcomes"][-1]
    if (state["call"].get("model_dossier_id") or 0) > 0 and not last.get("reused"):
        return "configuration"
    return "fichiers"


async def configuration(state: Run):
    """Graph node: copy the model dossier's configuration onto the target dossier (copy_configuration)."""
    copied = await run_step(
        state, "copy_configuration", dossier_id=state["target"], model_dossier_id=state["call"]["model_dossier_id"]
    )
    return {"outcomes": [copied]}


async def creator(state: Run):
    """Graph node: check the sender may create dossiers: the cabinet's managers and Experio's staff only
    (check_creator)."""
    return {"outcomes": [await run_step(state, "check_creator", session_id=state["call"]["session_id"])]}


@stops_at("resume")
def after_creator(state: Run):
    """Route: "fichiers" for mode dossiers (a spreadsheet), else "creation"; "resume" when the check failed."""
    if failed(state):
        return "resume"
    return "fichiers" if state["call"].get("mode") == "dossiers" else "creation"


async def manager(state: Run):
    """Graph node: check the sender may manage people in Experio (check_manager)."""
    return {"outcomes": [await run_step(state, "check_manager", session_id=state["call"]["session_id"])]}


@stops_at("resume")
def after_manager(state: Run):
    """Route to "acces" (mode person), "droits" (mode access) or "routine" (mode routine); "resume" when the check
    failed."""
    if failed(state):
        return "resume"
    return {"person": "acces", "routine": "routine"}.get(state["call"].get("mode"), "droits")


async def access(state: Run):
    """Graph node: change a person's access (change_access)."""
    call = state["call"]
    result = await run_step(
        state,
        "change_access",
        action=call.get("action") or "",
        email=call.get("email") or "",
        dossier_id=call.get("dossier_id") or 0,
        profile=call.get("profile") or "",
        client_confirmed=call.get("client_confirmed") is True,
        client_message=call.get("client_message") or "",
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


async def routine(state: Run):
    """Graph node: make, list, pause, resume or delete a routine (manage_routine)."""
    call = state["call"]
    result = await run_step(
        state,
        "manage_routine",
        action=call.get("action") or "",
        kind=call.get("routine_kind") or "",
        every=call.get("every") or "",
        days=call.get("days") or [],
        hour=call.get("hour") if call.get("hour") is not None else 8,
        routine_id=call.get("routine_id") or 0,
        email=call.get("email") or "",
        prompt=call.get("prompt") or "",
        client_confirmed=call.get("client_confirmed") is True,
        client_message=call.get("client_message") or "",
        session_id=call["session_id"],
    )
    return {"outcomes": [result]}


async def sender(state: Run):
    """Graph node: check the sender may work on the call's dossier (check_sender)."""
    return await sender_checked(state, state["call"]["dossier_id"])


@stops_at("resume")
def after_sender(state: Run):
    """Route to the mode's change node (modification, archivage, ecart), else "fichiers" (an existing dossier's
    onboarding); "resume" when the check failed."""
    if failed(state):
        return "resume"
    return {
        "update": "modification",
        "archive": "archivage",
        "difference": "ecart",
    }.get(state["call"].get("mode"), "fichiers")


async def update(state: Run):
    """Graph node: change the dossier's identity fields given in the call (update_dossier)."""
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
    """Graph node: create a person's access to the dossier (create_person)."""
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
    """Graph node: archive the dossier (archive_dossier)."""
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
    """Graph node: write off the remainder of the posted invoice the client named (settle_difference)."""
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
    """Graph node: set "files" to the message's files to import (files_to_process): spreadsheets of dossiers in mode
    dossiers, else files of entries for the dossier."""
    found = await run_step(state, "files_to_process", session_id=state["call"]["session_id"], entries=True)
    return {"files": found.get("files", []), "outcomes": [found]}


@stops_at("resume")
def after_files(state: Run):
    """Route: "listes" in mode dossiers, else "ecritures"; "resume" when listing the files failed."""
    if failed(state):
        return "resume"
    return "listes" if state["call"].get("mode") == "dossiers" else "ecritures"


async def lists(state: Run):
    """Graph node (mode dossiers): import each spreadsheet's dossiers (import_dossiers); they are created only once the
    client confirmed the list."""
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
    """Graph node: import each file's entries into the target dossier (import_entries)."""
    call = state["call"]
    outcomes = [
        await run_step(
            state, "import_entries", dossier_id=state["target"], file_id=file_id, kind=call.get("kind") or ""
        )
        for file_id in state.get("files", [])
    ]
    return {"outcomes": outcomes}


@stops_at("resume")
def after_entries(state: Run):
    """Route to "banques" unless an import was not ok, then to "resume" (the client sends that file again first)."""
    imported = state["outcomes"][-len(state["files"]) :] if state.get("files") else []
    if any(result.get("status") != "ok" for result in imported):
        return "resume"
    return "banques"


async def banks(state: Run):
    """Graph node: ask the bank of each of the dossier's bank journals (ask_banks), and queue the dossier for
    finish_setup with no time limit."""
    asked = await run_step(state, "ask_banks", dossier_id=state["target"])
    options = {"seconds_left": 0, "finish": state["call"].get("finish") is True}
    return {"outcomes": [asked], "finishing": [state["target"]], "finish_options": options}


async def resume_setups(state: Run):
    """Graph node (mode resume): queue the conversation's dossiers left waiting (waiting_setups) for finish_setup."""
    found = await run_step(state, "waiting_setups", session_id=state["call"]["session_id"])
    return {"outcomes": [found], "finishing": found.get("dossiers", []), "finish_options": {}}


async def finish(state: Run):
    """Graph node: finish each queued dossier's onboarding (finish_setup); "finished" lists those with an ok result."""
    outcomes, done = [], []
    for dossier_id in state.get("finishing", []):
        result = await run_step(state, "finish_setup", dossier_id=dossier_id, **state.get("finish_options", {}))
        outcomes.append(result)
        if result.get("status") == "ok":
            done.append(dossier_id)
    return {"outcomes": outcomes, "finished": done}


@stops_at("resume")
def after_finish(state: Run):
    """Route to "etat" when some dossier was finished, else "resume"."""
    return "etat" if state.get("finished") else "resume"


async def status(state: Run):
    """Graph node: report each finished dossier's state (setup_status)."""
    outcomes = [await run_step(state, "setup_status", dossier_id=d) for d in state.get("finished", [])]
    return {"outcomes": outcomes}


run = StateGraph(Run)
for name, node in {
    "debut": start,
    "createur": creator,
    "creation": create,
    "configuration": configuration,
    "gestionnaire": manager,
    "expediteur": sender,
    "modification": update,
    "acces": person,
    "droits": access,
    "routine": routine,
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
run.add_conditional_edges("debut", by_mode, ["createur", "gestionnaire", "expediteur", "reprise", "resume"])
run.add_conditional_edges("gestionnaire", after_manager, ["acces", "droits", "routine", "resume"])
run.add_conditional_edges("createur", after_creator, ["creation", "fichiers", "resume"])
run.add_conditional_edges("creation", after_create, ["configuration", "fichiers", "resume"])
run.add_conditional_edges("configuration", *ok_or_summary("fichiers"))  # a refused copy goes no further
run.add_conditional_edges("expediteur", after_sender, ["modification", "archivage", "ecart", "fichiers", "resume"])
run.add_conditional_edges("fichiers", after_files, ["listes", "ecritures", "resume"])
run.add_conditional_edges("ecritures", after_entries, ["banques", "resume"])
run.add_edge("banques", "finalisation")
run.add_edge("reprise", "finalisation")
run.add_conditional_edges("finalisation", after_finish, ["etat", "resume"])
for name in ("modification", "acces", "droits", "routine", "archivage", "ecart", "listes", "etat"):
    run.add_edge(name, "resume")
run.add_edge("resume", END)
graph = run.compile(name="Tarik - Dossier")
