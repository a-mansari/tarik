"""The routines' trigger: once a minute, each served cabinet's due routines (engine/tools/dossiers/routines.py) start
a run of the Routine workflow (engine/routine.py), shown on the Executions page like any run.

A due routine's period is recorded first (tarik_routine_runs: the same period never runs twice) and its next time
set, in one transaction that locks it (for update skip locked); then its run starts in the background. A period
started and never reported (the server stopped meanwhile: its run ended as cut off) starts a new run at the next
tick: the declarations it already made are only reported. A routine missed while the server was down runs once when
it is back, then at its next time. One routine at a time per cabinet; the run's report is kept with its period.
"""

import asyncio
import time
from datetime import datetime

from engine import runs
from engine.calls import within
from engine.tools.core.app import CRASHED, log
from engine.tools.core.cabinets import CABINETS, cabinet_db
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import ZONE, next_run, session_of, subject_of, title_of

TICK_SECONDS = 60
ONE_PER_CABINET = {}  # cabinet -> its semaphore of 1
RUNNING = set()  # (cabinet, routine id, due_at) of the runs under way in this process: never started twice
PENDING = set()  # their tasks (a task nobody holds may be collected before it ends)


def claimed():
    """Return the current cabinet's routines to run now, as (id, kind, every, days, hour, minute, notify, due_at,
    prompt, name): the periods started and never reported, then the due routines, each with its period taken and its
    next time set (one whose period already ran is skipped, its next time set all the same)."""
    with cabinet_db() as db, db.transaction():
        taken = db.execute(Q.routines_unfinished).fetchall()
        for row in db.execute(Q.routines_due).fetchall():
            routine_id, _, every, days, hour, minute, _, due_at, *_ = row
            upcoming = next_run(every, days, hour, minute, max(due_at, datetime.now(ZONE)))
            db.execute(Q.routine_next, (upcoming, routine_id))
            if db.execute(Q.routine_period_taken, (routine_id, due_at)).fetchone():
                taken.append(row)
    return taken


def given(cabinet, row):
    """Return the Routine run's input for claimed routine ``row``: its call, in the routine's own session."""
    routine_id, kind, every, days, hour, minute, notify, due_at, prompt, name = row
    schedule, title = (every, days, hour, minute), title_of(routine_id, kind, name)
    call = {
        "routine_id": routine_id,
        "routine_name": title,  # what it was called then: shown after it is renamed or deleted
        "task": kind,
        "prompt": prompt,
        "notify": notify,
        "subject": subject_of(title, schedule),
        "until": next_run(*schedule, due_at).isoformat(),  # its next period: the dossiers not reached are left to it
        "session_id": session_of(routine_id, due_at),
        "cabinet": cabinet,
    }
    return {"call": call}


STOPPED = "Routine arrêtée à la main pendant son exécution."


async def ran(cabinet, row):
    """Run one claimed routine of ``cabinet`` as a Routine run (one at a time per cabinet) and keep its report with
    its period; return the report. A failed run's period is reported all the same: never run again in a loop."""
    routine_id, due_at = row[0], row[7]
    async with ONE_PER_CABINET.setdefault(cabinet, asyncio.Semaphore(1)):
        try:
            # No run slot held for hours: its dossiers take turns in routine.EVERYWHERE, the clients' runs never wait
            _, task = await runs.start("routine", {"input": given(cabinet, row)}, slots=None, unattended=True)
            output, failure = await task
            report = CRASHED if failure else (output or {}).get("message") or CRASHED
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # the server stopping: its period runs again at the next start
                raise
            report = STOPPED  # stopped by hand (runs.stop): reported, never run again
        except Exception as error:
            log(f"{cabinet} routine {routine_id}: {type(error).__name__}: {error}")
            report = CRASHED
        await within(cabinet, record, routine_id, due_at, report)
    return report


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
