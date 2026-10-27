"""The routines' clock: once a minute, each served cabinet's due routines (engine/tools/dossiers/routines.py) run.

A due routine's period is recorded first (tarik_routine_runs: the same period never runs twice) and its next time
set, in one transaction that locks it (for update skip locked); then it runs in the background. A period started and
never reported (the server stopped meanwhile) is run again by the next tick: the declarations it already made are
only reported. A routine missed while the server was down runs once when it is back, then at its next time.

A run: the dossiers its address may still see (allowed_dossiers, checked again each time; none left pauses it);
« tva » and « ras » start a Déclarations run per dossier in its routine mode (a declaration made, none taken up nor
declared), « progress » reads each dossier's progress; the report, with the files the runs made (the XML), goes by
email to the routine's ``notify``. One routine at a time per cabinet, at most RUNNING_AT_ONCE across cabinets: a
cabinet's 200 routines never take the slots its clients' own runs need.
"""

import asyncio
from datetime import datetime

from engine import runs
from engine.calls import ToolFailure, call, within
from engine.tools.core.app import CRASHED, log
from engine.tools.core.cabinets import CABINETS, cabinet_db
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import KINDS, ZONE, allowed_dossiers, next_run, when

TICK_SECONDS = 60
RUNNING_AT_ONCE = 2  # routines running at once, all cabinets together (runs.SLOTS holds 20 runs)
EVERYWHERE = asyncio.Semaphore(RUNNING_AT_ONCE)
ONE_PER_CABINET = {}  # cabinet -> its semaphore of 1
RUNNING = set()  # (cabinet, routine id, due_at) of the runs under way in this process: never started twice
PENDING = set()  # their tasks (a task nobody holds may be collected before it ends)


def claimed():
    """Return the current cabinet's routines to run now, as (id, kind, dossier_ids, every, day, hour, notify, due_at):
    the periods started and never reported, then the due routines, each with its period taken and its next time set
    (one whose period already ran is skipped, its next time set all the same)."""
    with cabinet_db() as db, db.transaction():
        taken = db.execute(Q.routines_unfinished).fetchall()
        for row in db.execute(Q.routines_due).fetchall():
            routine_id, _, _, every, day, hour, _, due_at = row
            db.execute(Q.routine_next, (next_run(every, day, hour, max(due_at, datetime.now(ZONE))), routine_id))
            if db.execute(Q.routine_period_taken, (routine_id, due_at)).fetchone():
                taken.append(row)
    return taken


async def ran(cabinet, row):
    """Run one claimed routine of ``cabinet`` (one at a time per cabinet, RUNNING_AT_ONCE in all), email its report
    and keep it; return the report. A dossier's failure is a line of the report, never the end of the others."""
    routine_id, kind, dossier_ids, every, day, hour, notify, due_at = row
    async with ONE_PER_CABINET.setdefault(cabinet, asyncio.Semaphore(1)), EVERYWHERE:
        try:
            allowed = await within(cabinet, allowed_dossiers, notify, dossier_ids)
            if not allowed:
                await within(cabinet, paused, routine_id)
                report = f"Routine en pause : `{notify}` n'a plus aucun de ses dossiers dans Experio."
            else:
                report = await reported(cabinet, row, allowed)
        except Exception as error:  # the period stays reported: never run again in a loop
            log(f"{cabinet} routine {routine_id}: {type(error).__name__}: {error}")
            report = CRASHED
        await within(cabinet, record, routine_id, due_at, report)
    return report


async def reported(cabinet, row, allowed):
    """Run routine ``row`` on its ``allowed`` dossiers, email the report with the runs' files, and return it."""
    routine_id, kind, dossier_ids, every, day, hour, notify, due_at = row
    session = f"routine-{routine_id}-{due_at:%Y%m%d%H}"  # its runs, together on the Executions page
    parts = []
    for dossier_id in allowed:
        try:
            parts.append(await dossier_part(cabinet, kind, dossier_id, session))
        except Exception as error:  # the cause is logged, the client reads Tarik's usual words
            log(f"{cabinet} routine {routine_id} dossier {dossier_id}: {type(error).__name__}: {error}")
            parts.append(f"Dossier n° {dossier_id} : {error if isinstance(error, ToolFailure) else CRASHED}")
    left = len(dossier_ids) - len(allowed)
    if left:
        parts.append(f"{left} dossier(s) laissé(s) de côté : `{notify}` ne les a plus dans Experio.")
    report = "\n\n".join(parts)
    subject = f"Routine n° {routine_id} : {KINDS[kind]} ({when(every, day, hour)})"
    try:
        await call(cabinet, "send_email", to=notify, subject=subject, body=report, attach_session=session)
    except ToolFailure as failure:
        log(f"{cabinet} routine {routine_id}: report not sent: {failure}")
    return report


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
    key = (cabinet, row[0], row[7])
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
    """Run tick() once a minute for the server's whole life."""
    while True:
        try:
            await tick()
        except Exception as error:  # never stop the clock
            log(f"routines: {type(error).__name__}: {error}")
        await asyncio.sleep(TICK_SECONDS)
