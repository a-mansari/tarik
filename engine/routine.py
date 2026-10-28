"""The Routine graph: one period of a cabinet's routine (engine/tools/dossiers/routines.py), started by its schedule
(engine/routines.py, the routines' trigger) and shown on the Executions page like any run.

Its report's address is checked again first (live: someone gone pauses the routine, nothing is sent). An « instruction »
routine hands its words to the agent once (a Discussion run in the routine's session: Tarik's reach, nothing confirmed,
only the workflows that prepare). The other kinds run on each dossier Tarik's Experio account may open, one step per
dossier: « tva » and « ras » a Déclarations run in its routine mode (a declaration made, none taken up nor declared),
« progress » the dossier's progress. A dossier's failure is a line of the report, never the end of the others; the
dossiers not reached when the next period comes (``until``) are counted, left to it. The report goes by email to
the routine's ``notify``: a few lines in its body, every dossier in the Excel, CSV and text files joined, with the
files the runs made (the XML). At most RUNNING_AT_ONCE dossiers or instructions run
at once across cabinets: a cabinet's routines never take the slots its clients' own runs need."""

import asyncio
import operator
from datetime import datetime
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from engine import agent
from engine.calls import ToolFailure, call, failed, run_step, started, stops_at, within
from engine.tools.core.app import CRASHED, log
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.outbox import keep_file
from engine.tools.core.queries import Q
from engine.tools.dossiers import routine_report as report_files
from engine.tools.dossiers.routines import ZONE, address_problem
from engine.tools.dossiers.search import opened_dossiers
from engine.tools.emails.inbox import opened_thread, thread_of_session
from engine.tools.invoices.pipeline import NOTHING

INPUTS = ["routine_id", "task", "prompt", "notify", "subject", "until", "session_id", "cabinet"]
RUNNING_AT_ONCE = 2  # dossiers or instructions running at once, all cabinets together (runs.SLOTS holds 20 runs)
INSTRUCTION_SECONDS = 30 * 60  # an instruction's agent run is cut off after this: the other routines wait for it
BODY_CHARACTERS = 20_000  # an instruction's reply longer than this goes as a text file, its start in the email
EVERYWHERE = asyncio.Semaphore(RUNNING_AT_ONCE)


class Run(TypedDict, total=False):
    call: dict
    run_id: str
    outcomes: Annotated[list, operator.add]
    dossiers: list  # [{id, name}] the routine goes through
    dossier: dict  # one of them, in its own step
    parts: Annotated[list, operator.add]  # the instruction's reply, or each dossier's {dossier, result, detail, ...}
    report: str
    instructed: bool  # an instruction's agent replied (its conversation ends on that reply, nothing left open)
    message: str


def paused(routine_id):
    """Pause routine ``routine_id`` (routine_paused)."""
    with cabinet_db() as db:
        db.execute(Q.routine_paused, (routine_id,))


def broke(given, step, error):
    """Return the error outcome of ``step`` failing: the cause logged, the page reads Tarik's usual words."""
    log(f"{given['cabinet']} routine {given['routine_id']} {step}: {type(error).__name__}: {error}")
    return {"outcomes": [{"status": "error", "message": CRASHED}]}


async def address(state: Run):
    """Graph node: check the report's address may still get it (address_problem); pause the routine when not."""
    given = state["call"]
    try:
        problem = await within(given["cabinet"], address_problem, given["notify"])
    except Exception as error:  # Experio unreachable: nothing sent this period
        return broke(given, "address", error)
    if not problem:
        return {"outcomes": [{"status": "ok", "message": "Destinataire vérifié."}]}
    await within(given["cabinet"], paused, given["routine_id"])  # someone gone, moved down, or no longer allowed
    return {"outcomes": [{"status": "stop", "message": f"Routine en pause : {problem}."}]}


@stops_at("fin")
def by_kind(state: Run):
    """Route an instruction to "consigne", another kind to "dossiers"; "fin" when the routine was paused or its
    address could not be checked."""
    if failed(state):
        return "fin"
    return "consigne" if state["call"]["task"] == "instruction" else "dossiers"


async def instruction(state: Run):
    """Graph node: hand the routine's words to the agent (a Discussion run in the routine's session, cut off after
    INSTRUCTION_SECONDS) and keep its reply for the report."""
    from engine import runs  # runs imports the graphs, this one among them

    given = state["call"]
    asked = {"cabinet": given["cabinet"], "session_id": given["session_id"], "message": given["prompt"]}
    status = "ok"
    try:
        talk = runs.nested(
            "discussion", {**asked, "files": [], "skipped": []}, given["cabinet"], given["session_id"], node="consigne"
        )
        async with EVERYWHERE:
            output = await asyncio.wait_for(talk, INSTRUCTION_SECONDS)
        reply = (output or {}).get("reply")
        if not reply:
            status, reply = "error", (output or {}).get("error") or "Tarik n'a rien répondu."
    except TimeoutError:
        status, reply = "error", f"Consigne interrompue après {INSTRUCTION_SECONDS // 60} minutes : à reprendre."
    return {"parts": [reply], "instructed": status == "ok", "outcomes": [{"status": status, "message": reply}]}


async def listed(state: Run):
    """Graph node: the dossiers Tarik's Experio account may open (opened_dossiers), the routine's to go through."""
    try:
        found = await within(state["call"]["cabinet"], opened_dossiers)
    except Exception as error:  # Experio unreachable: the report says so
        return broke(state["call"], "dossiers", error)
    return {"dossiers": found, "outcomes": [{"status": "ok", "message": f"{len(found)} dossier(s) à parcourir."}]}


def each_dossier(state: Run):
    """Route each dossier to its own "dossier" step; straight to the report when there is none."""
    if not state.get("dossiers"):
        return "rapport"
    given = {"call": state["call"], "run_id": state["run_id"]}
    return [Send("dossier", {**given, "dossier": dossier}) for dossier in state["dossiers"]]


async def progressed(given, dossier_id):
    """Return a « progress » routine's part for one dossier: where it stands, its text and its figures
    (dossier_progress_figures)."""
    result = await call(given["cabinet"], "dossier_progress_figures", dossier_id=dossier_id)
    return {"result": report_files.READ, "detail": result["message"], "figures": result["figures"]}


async def declared(given, dossier_id):
    """Return a « tva » or « ras » routine's part for one dossier: its Déclarations run's summary (routine mode),
    its result from the run's last step: prepared, already done, nothing to prepare, to see (a question, a stop) or
    failed."""
    from engine import runs  # runs imports the graphs, this one among them

    asked = {"mode": given["task"], "dossier_id": dossier_id, "session_id": given["session_id"]}
    run = {"call": {**asked, "cabinet": given["cabinet"], "routine": True}}
    output = await runs.nested("declarations", run, given["cabinet"], given["session_id"], node="dossier") or {}
    text = output.get("message") or f"Dossier n° {dossier_id} : rien à dire."
    last = (output.get("outcomes") or [{}])[-1]
    status = last.get("status")
    if text.strip() == NOTHING:
        result = report_files.NOTHING_TO_DO
    elif status == "ok" and last.get("already"):  # declared, or prepared before: nothing made this period
        result = report_files.ALREADY
    else:
        result = {"ok": report_files.PREPARED, "error": report_files.FAILED}.get(status, report_files.TO_SEE)
    return {"result": result, "detail": text}


PARTS = {"progress": progressed, "tva": declared, "ras": declared}  # a kind: its part of the report for one dossier


async def one_dossier(state: Run):
    """Graph node, once per dossier: its part of the report (PARTS), unless the next period came first (``until``).
    A failure is its line of the report: the cause is logged, the client reads Tarik's usual words."""
    given, dossier = state["call"], state["dossier"]
    left = {"dossier": dossier["name"], "result": report_files.LEFT, "detail": "La période suivante est arrivée avant."}
    late = {"parts": [left], "outcomes": [{"status": "stop", "message": f"{dossier['name']} : laissé."}]}
    if datetime.now(ZONE) >= datetime.fromisoformat(given["until"]):  # the next period: the rest is left to it
        return late
    async with EVERYWHERE:
        if datetime.now(ZONE) >= datetime.fromisoformat(given["until"]):  # it came while waiting for a turn
            return late
        try:
            part, status = await PARTS[given["task"]](given, dossier["id"]), "ok"
        except Exception as error:
            where = f"{given['cabinet']} routine {given['routine_id']} dossier {dossier['id']}"
            log(f"{where}: {type(error).__name__}: {error}")
            words = str(error) if isinstance(error, ToolFailure) else CRASHED
            part, status = {"result": report_files.FAILED, "detail": words}, "error"
    part["dossier"] = dossier["name"]
    return {"parts": [part], "outcomes": [{"status": status, "message": f"{dossier['name']} : {part['detail']}"}]}


async def report(state: Run):
    """Graph node: write the report. An instruction's reply is the email's body (kept as a text file when longer
    than BODY_CHARACTERS). On dossiers, the body is a few lines (routine_report.summary) and every dossier is in the
    files joined: Excel, CSV and text, kept in the routine's session with the runs' own files."""
    given, parts = state["call"], state.get("parts") or []
    cabinet, session, stem = given["cabinet"], given["session_id"], f"routine-{given['routine_id']}"
    if given["task"] == "instruction":
        text = parts[0] if parts else NOTHING
        if len(text) > BODY_CHARACTERS:  # the whole reply as a file, its start in the email
            await within(cabinet, keep_file, session, f"{stem}.txt", text.encode())
            text = text[:BODY_CHARACTERS] + "\n\n(Suite dans le fichier joint.)"
        return {"report": text, "outcomes": [{"status": "ok", "message": text}]}
    if not parts:  # no dossier opened, or Experio could not list them
        text = CRASHED if failed(state) else NOTHING
        return {"report": text, "outcomes": [{"status": "ok", "message": text}]}
    stem += f"-{datetime.now(ZONE):%Y-%m-%d}"
    header, rows = report_files.table(given["task"], parts)
    files = {
        f"{stem}.xlsx": report_files.workbook(header, rows),
        f"{stem}.csv": report_files.spreadsheet_text(header, rows),
        f"{stem}.txt": report_files.plain(parts).encode(),
    }
    for name, data in files.items():
        await within(cabinet, keep_file, session, name, data)
    text = report_files.summary(given["task"], parts, given["subject"], list(files))
    return {"report": text, "outcomes": [{"status": "ok", "message": text}]}


async def sent(state: Run):
    """Graph node: email the report, with the files the runs made, to the routine's ``notify`` (send_email). It is
    Tarik's own turn in the routine's conversation (agent.began; an instruction's reply already is) and the first
    email of a thread with them there (opened_thread): their reply is answered in that conversation by the usual
    Emails workflow, attended (the period's runs alone are unattended), with the report and its files before it."""
    given = state["call"]
    cabinet, session = given["cabinet"], given["session_id"]
    if await within(cabinet, thread_of_session, session):  # a period run again after its report went: never twice
        return {"outcomes": [{"status": "ok", "message": "Rapport déjà envoyé pour cette période."}]}
    email = {"to": given["notify"], "subject": given["subject"], "body": state["report"]}
    result = await run_step(state, "send_email", **email, attach_session=session)
    # An instruction's conversation is the reply's only when its agent replied: cut off, a tool call is left open there.
    joined = given["task"] != "instruction" or state.get("instructed") is True
    if result.get("status") == "ok" and result.get("message_id") and joined:
        try:  # first: without it a reply starts a thread of its own
            await within(cabinet, opened_thread, result["message_id"], email["to"], email["subject"], session)
        except Exception as error:
            log(f"{cabinet} routine {given['routine_id']} thread: {type(error).__name__}: {error}")
        try:
            if given["task"] != "instruction":
                await agent.began(cabinet, session, email["subject"], email["body"])
        except Exception as error:  # the reply is answered all the same, the report's files at hand
            log(f"{cabinet} routine {given['routine_id']} memory: {type(error).__name__}: {error}")
    return {"outcomes": [result]}


def ended(state: Run):
    """Graph node: the run's message: the report sent, or why there is none (the routine paused)."""
    return {"message": state.get("report") or state["outcomes"][-1].get("message", "")}


run = StateGraph(Run)
for name, node in {
    "debut": started(INPUTS),
    "adresse": address,
    "consigne": instruction,
    "dossiers": listed,
    "dossier": one_dossier,
    "rapport": report,
    "envoi": sent,
    "fin": ended,
}.items():
    run.add_node(name, node)
run.add_edge(START, "debut")
run.add_edge("debut", "adresse")
run.add_conditional_edges("adresse", by_kind, ["consigne", "dossiers", "fin"])
run.add_edge("consigne", "rapport")
run.add_conditional_edges("dossiers", each_dossier, ["dossier", "rapport"])
run.add_edge("dossier", "rapport")
run.add_edge("rapport", "envoi")
run.add_edge("envoi", "fin")
run.add_edge("fin", END)
# One dossier at a time per routine: each routine waits for EVERYWHERE with one dossier, so cabinets take turns
graph = run.compile(name="Tarik - Routine").with_config(max_concurrency=1)
