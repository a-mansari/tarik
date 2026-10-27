"""The routines' clock: once a minute, each served cabinet's due routines (engine/tools/dossiers/routines.py) run.

A due routine's period is recorded first (tarik_routine_runs: the same period never runs twice) and its next time
set, in one transaction that locks it (for update skip locked); then it runs in the background. A period started and
never reported (the server stopped meanwhile) is run again by the next tick: the declarations it already made are
only reported. A routine missed while the server was down runs once when it is back, then at its next time.

A run: its address checked again (live: someone gone pauses it); « tva » and « ras » start a Déclarations run in
its routine mode (a declaration made, none taken up nor declared) and « progress » reads the progress, for each
dossier Tarik's Experio account sees; « instruction » hands its words to the agent once (a Discussion run in the
routine's session: Tarik's reach, nothing confirmed, only the workflows that prepare); the report, with the files the
runs made (the XML), goes by email to the routine's ``notify``. One routine at a time per cabinet, at most
RUNNING_AT_ONCE across cabinets: a cabinet's 200 routines never take the slots its clients' own runs need.
"""

import asyncio
import time
from datetime import datetime

from engine import runs
from engine.calls import ToolFailure, call, within
from engine.tools.core.app import CRASHED, log
from engine.tools.core.cabinets import CABINETS, cabinet_db
from engine.tools.core.outbox import keep_file
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import KINDS, ZONE, address_problem, next_run, session_of, when
from engine.tools.dossiers.search import opened_dossiers
from engine.tools.invoices.pipeline import NOTHING

TICK_SECONDS = 60
RUNNING_AT_ONCE = 2  # routines running at once, all cabinets together (runs.SLOTS holds 20 runs)
INSTRUCTION_SECONDS = 30 * 60  # an instruction's agent run is cut off after this: the other routines wait for it
BODY_CHARACTERS = 20_000  # a report longer than this goes as a text file, its start in the email and on the page
EVERYWHERE = asyncio.Semaphore(RUNNING_AT_ONCE)
ONE_PER_CABINET = {}  # cabinet -> its semaphore of 1
RUNNING = set()  # (cabinet, routine id, due_at) of the runs under way in this process: never started twice
PENDING = set()  # their tasks (a task nobody holds may be collected before it ends)


def claimed():
    """Return the current cabinet's routines to run now, as (id, kind, every, days, hour, minute, notify, due_at,
    prompt): the
    periods started and never reported, then the due routines, each with its period taken and its next time set
    (one whose period already ran is skipped, its next time set all the same)."""
    with cabinet_db() as db, db.transaction():
        taken = db.execute(Q.routines_unfinished).fetchall()
        for row in db.execute(Q.routines_due).fetchall():
            routine_id, _, every, days, hour, minute, _, due_at, _ = row
            upcoming = next_run(every, days, hour, minute, max(due_at, datetime.now(ZONE)))
            db.execute(Q.routine_next, (upcoming, routine_id))
            if db.execute(Q.routine_period_taken, (routine_id, due_at)).fetchone():
                taken.append(row)
    return taken


async def ran(cabinet, row):
    """Run one claimed routine of ``cabinet`` (one at a time per cabinet, RUNNING_AT_ONCE in all), email its report
    and keep it; return the report. A dossier's failure is a line of the report, never the end of the others."""
    routine_id, _, _, _, _, _, notify, due_at, _ = row
    async with ONE_PER_CABINET.setdefault(cabinet, asyncio.Semaphore(1)):
        try:
            problem = await within(cabinet, address_problem, notify)
            if problem:  # someone gone, moved down, or no longer allowed: nothing more sent to them
                await within(cabinet, paused, routine_id)
                report = f"Routine en pause : {problem}."
            else:
                report = await reported(cabinet, row)
        except Exception as error:  # the period stays reported: never run again in a loop
            log(f"{cabinet} routine {routine_id}: {type(error).__name__}: {error}")
            report = CRASHED
        await within(cabinet, record, routine_id, due_at, report)
    return report


async def reported(cabinet, row):
    """Run routine ``row`` (an instruction once, another kind on each dossier Tarik sees, until its next time comes),
    email the report with the runs' files, and return it (its start when long: BODY_CHARACTERS). Dossiers with
    nothing to say are counted, not listed; each part takes a slot of EVERYWHERE, so cabinets take turns."""
    routine_id, kind, every, days, hour, minute, notify, due_at, prompt = row
    session = session_of(routine_id, due_at)
    deadline = next_run(every, days, hour, minute, due_at)
    parts, quiet, left = [], 0, 0
    if kind == "instruction":
        async with EVERYWHERE:
            parts.append(await instructed(cabinet, prompt, session))
    else:
        for dossier_id in [dossier["id"] for dossier in await within(cabinet, opened_dossiers)]:
            if datetime.now(ZONE) >= deadline:  # its next period is due: the rest waits for it
                left += 1
                continue
            try:
                async with EVERYWHERE:
                    part = await dossier_part(cabinet, kind, dossier_id, session)
            except Exception as error:  # the cause is logged, the client reads Tarik's usual words
                log(f"{cabinet} routine {routine_id} dossier {dossier_id}: {type(error).__name__}: {error}")
                part = f"Dossier n° {dossier_id} : {error if isinstance(error, ToolFailure) else CRASHED}"
            if part.strip() == NOTHING:
                quiet += 1
            else:
                parts.append(part)
    if quiet:
        parts.append(f"{quiet} dossier(s) sans rien à préparer.")
    if left:
        parts.append(f"{left} dossier(s) non traité(s) : la période suivante est arrivée avant.")
    report = "\n\n".join(parts) or NOTHING
    subject = f"Routine n° {routine_id} : {KINDS[kind]} ({when(every, days, hour, minute)})"
    body = report
    if len(report) > BODY_CHARACTERS:  # the whole report as a file, its start in the email
        await within(cabinet, keep_file, session, f"routine-{routine_id}.txt", report.encode())
        body = report[:BODY_CHARACTERS] + "\n\n(Suite dans le fichier joint.)"
    try:
        await call(cabinet, "send_email", to=notify, subject=subject, body=body, attach_session=session)
    except ToolFailure as failure:
        log(f"{cabinet} routine {routine_id}: report not sent: {failure}")
    return body


async def instructed(cabinet, prompt, session):
    """Hand an instruction routine's ``prompt`` to the agent in the routine's ``session`` (a Discussion run, cut off
    after INSTRUCTION_SECONDS) and return its reply."""
    given = {"cabinet": cabinet, "session_id": session, "message": prompt, "files": [], "skipped": []}
    _, task = await runs.start("discussion", {"input": given, "session_id": session, "cabinet": cabinet})
    try:
        output, failure = await asyncio.wait_for(task, INSTRUCTION_SECONDS)
    except TimeoutError:
        return f"Consigne interrompue après {INSTRUCTION_SECONDS // 60} minutes : à reprendre dans une discussion."
    if failure:
        raise failure
    return (output or {}).get("reply") or (output or {}).get("error") or "Tarik n'a rien répondu."


async def dossier_part(cabinet, kind, dossier_id, session):
    """Return the report's part for one dossier: its progress, or its Déclarations run's summary (routine mode)."""
    if kind == "progress":
        return await call(cabinet, "dossier_progress", dossier_id=dossier_id)
    given = {"call": {"mode": kind, "dossier_id": dossier_id, "session_id": session, "cabinet": cabinet}}
    given["call"]["routine"] = True
    _, task = await runs.start("declarations", {"input": given, "session_id": session, "cabinet": cabinet})
    output, failure = await task
    if failure:
        raise failure
    return (output or {}).get("message") or f"Dossier n° {dossier_id} : rien à dire."


def paused(routine_id):
    """Pause routine ``routine_id`` (routine_paused)."""
    with cabinet_db() as db:
        db.execute(Q.routine_paused, (routine_id,))


def record(routine_id, due_at, report):
    """Keep what a routine's run reported (routine_reported): the period is then done."""
    with cabinet_db() as db:
        db.execute(Q.routine_reported, (report, routine_id, due_at))


def started(cabinet, row):
    """Start claimed routine ``row`` of ``cabinet`` in the background, unless that period already runs here."""
    key = (cabinet, row[0], row[7])  # its id and period (due_at)
    if key in RUNNING:
        return
    RUNNING.add(key)
    task = asyncio.create_task(ran(cabinet, row))
    PENDING.add(task)
    task.add_done_callback(lambda done: (PENDING.discard(done), RUNNING.discard(key)))


async def tick():
    """Start every served cabinet's routines to run now; a cabinet's failure is only logged."""
    for cabinet in sorted(CABINETS):
        try:
            due = await within(cabinet, claimed)
        except Exception as error:
            log(f"{cabinet}: routines not read: {type(error).__name__}: {error}")
            continue
        for row in due:
            started(cabinet, row)


async def clock():
    """Run tick() at the start of each minute for the server's whole life."""
    while True:
        try:
            await tick()
        except Exception as error:  # never stop the clock
            log(f"routines: {type(error).__name__}: {error}")
        await asyncio.sleep(TICK_SECONDS - time.time() % TICK_SECONDS)  # on the minute: a routine's minute kept
