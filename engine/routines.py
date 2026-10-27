"""The routines' clock: once a minute, each served cabinet's due routines (engine/tools/dossiers/routines.py) run.

A due routine's period is recorded first (tarik_routine_runs: the same period never runs twice, after a restart
included) and its next time set, in one transaction that locks it (for update skip locked); then it runs in the
background: « tva » and « ras » start a Déclarations run per dossier (prepared, never declared), « progress » reads
each dossier's progress; the report goes by email to the routine's ``notify``. A routine missed while the server was
down runs once when it is back, then at its next time.
"""

import asyncio
from datetime import datetime

from engine import runs
from engine.calls import ToolFailure, call, within
from engine.tools.core.app import log
from engine.tools.core.cabinets import CABINETS, cabinet_db
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import KINDS, ZONE, next_run, when

TICK_SECONDS = 60
PENDING = set()  # the routines running (a task nobody holds may be collected before it ends)


def claimed():
    """Return the current cabinet's due routines (routines_due rows), each with its period taken and its next time
    set; those whose period already ran are skipped (their next time set all the same)."""
    with cabinet_db() as db, db.transaction():
        due = db.execute(Q.routines_due).fetchall()
        taken = []
        for row in due:
            routine_id, _, _, every, day, hour, _, due_at = row
            db.execute(Q.routine_next, (next_run(every, day, hour, max(due_at, datetime.now(ZONE))), routine_id))
            if db.execute(Q.routine_period_taken, (routine_id, due_at)).fetchone():
                taken.append(row)
    return taken


async def ran(cabinet, row):
    """Run one claimed routine of ``cabinet`` and email its report; return the report. A dossier's failure is part
    of the report, never the end of the others."""
    routine_id, kind, dossier_ids, every, day, hour, notify, due_at = row
    session = f"routine-{routine_id}-{due_at:%Y%m%d%H}"  # its runs, together on the Executions page
    parts = []
    for dossier_id in dossier_ids:
        try:
            if kind == "progress":
                parts.append(await call(cabinet, "dossier_progress", dossier_id=dossier_id))
                continue
            given = {"call": {"mode": kind, "dossier_id": dossier_id, "session_id": session, "cabinet": cabinet}}
            _, task = await runs.start("declarations", {"input": given, "session_id": session, "cabinet": cabinet})
            output, failure = await task
            parts.append((output or {}).get("message") or f"Dossier n° {dossier_id} : échec technique ({failure}).")
        except ToolFailure as failure:
            parts.append(f"Dossier n° {dossier_id} : {failure}")
    report = "\n\n".join(parts)
    subject = f"Routine n° {routine_id} : {KINDS[kind]} ({when(every, day, hour)})"
    try:
        await call(cabinet, "send_email", to=notify, subject=subject, body=report)
    except ToolFailure as failure:
        log(f"{cabinet} routine {routine_id}: report not sent: {failure}")
    await within(cabinet, record, routine_id, due_at, report)
    return report


def record(routine_id, due_at, report):
    """Keep what a routine's run reported (routine_reported)."""
    with cabinet_db() as db:
        db.execute(Q.routine_reported, (report, routine_id, due_at))


async def tick():
    """Start every served cabinet's due routines, each in the background; a cabinet's failure is only logged."""
    for cabinet in sorted(CABINETS):
        try:
            due = await within(cabinet, claimed)
        except Exception as error:
            log(f"{cabinet}: routines not read: {type(error).__name__}: {error}")
            continue
        for row in due:
            task = asyncio.create_task(ran(cabinet, row))
            PENDING.add(task)
            task.add_done_callback(PENDING.discard)


async def clock():
    """Run tick() once a minute for the server's whole life."""
    while True:
        try:
            await tick()
        except Exception as error:  # never stop the clock
            log(f"routines: {type(error).__name__}: {error}")
        await asyncio.sleep(TICK_SECONDS)
