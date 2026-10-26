"""The mail watcher: read each cabinet's Tarik mailbox (its own IMAP_USER) the moment an email arrives, and every 15
minutes in case one was missed.

Each read stores the new emails in their threads, notifies the cabinet's manager of refused senders, and starts an
Emails run (for that cabinet only) when a thread waits for an answer. Watching starts with the server for every
cabinet with its own mailbox, and for a cabinet the Experio API creates or reactivates (start_one). A deactivated
cabinet is no longer served at once; its watcher threads end at their next wake-up.

Per cabinet, one IMAP IDLE connection to its Tarik folder (read-only; certificate pinned when MAIL_CERT_SHA256 is
set, as read_mailbox connects): the IMAP server signals a new email and the watcher reads it. The connection is
renewed every 25 minutes (servers drop an IDLE after ~30) and rebuilt after any error; the 15-minute tick still
catches whatever happens meanwhile.
"""

import asyncio
import threading
import time

from engine.tools.core.app import log
from engine.tools.core.cabinets import CABINETS, setting, using
from engine.tools.emails.inbox import connect, notify_manager, read_mailbox, threads_waiting

RENEW_SECONDS = 25 * 60
RUNNING = set()  # (cabinet, job name) of each watch and tick thread alive
TICK_SECONDS = 15 * 60
LOOP = None  # the server's event loop, where the runs start (set by start())


def wait_for_mail(box, count, seconds):
    """IDLE on the open folder ``box``, holding ``count`` emails, until one more arrives or ``seconds`` pass.

    Returns (arrived, count): arrived is True when the folder grew, count is the folder's latest email count."""
    with box.idle(duration=seconds) as idler:
        for kind, data in idler:
            if kind == "EXPUNGE":  # one deleted elsewhere: the count to beat goes down with it
                count -= 1
            elif kind == "EXISTS":
                if int(data[0]) > count:
                    return True, int(data[0])
                count = int(data[0])
    return False, count


def wake_tarik(cabinet, why):
    """Start an Emails run for ``cabinet``, logging ``why`` (nothing when ""). A failure is only logged."""
    from engine import runs  # runs imports the graphs, which import the tools: here, once all are loaded

    try:
        started = runs.start("emails", {"input": {"cabinet": cabinet}, "cabinet": cabinet})
        asyncio.run_coroutine_threadsafe(started, LOOP).result(30)
        if why:
            log(f"{cabinet}: {why}, Emails run started")
    except Exception as error:  # the next tick tries again
        log(f"{cabinet}: Emails run not started: {type(error).__name__}")


def serve(cabinet, why):
    """Store the cabinet's new emails, notify its manager of refused senders, and start an Emails run (wake_tarik,
    logging ``why``) when a thread waits. Must run within using(cabinet); does nothing for a deactivated cabinet.
    Every failure is only logged: the next wake-up or tick tries again."""
    if cabinet not in CABINETS:  # deactivated meanwhile: nothing read, nothing answered
        return
    try:
        alerts = read_mailbox()
    except Exception as error:
        log(f"{cabinet}: mailbox not read: {type(error).__name__}: {error}")
        alerts = []
    for alert in alerts:
        try:
            notify_manager(**alert)
        except Exception as error:
            log(f"{cabinet}: manager not told of {alert['sender']}: {error}")
    try:
        waiting = threads_waiting()
    except Exception as error:
        log(f"{cabinet}: threads not checked: {type(error).__name__}")
        waiting = False
    if waiting:
        wake_tarik(cabinet, why)


def watch(cabinet):
    """Keep an IDLE connection on the cabinet's mailbox and serve it each time an email arrives, reconnecting 30 s
    after any error, until the cabinet is deactivated."""
    with using(cabinet):
        while cabinet in CABINETS:
            try:
                with connect() as box:
                    count = int(box.untagged_responses.get("EXISTS", [b"0"])[-1])
                    log(f"{cabinet}: watching ({count} emails)")
                    while cabinet in CABINETS:
                        arrived, count = wait_for_mail(box, count, RENEW_SECONDS)
                        if arrived:
                            serve(cabinet, "new email")
            except Exception as error:
                log(f"{cabinet}: watch interrupted: {error}; reconnecting in 30 s")
                time.sleep(30)


def tick(cabinet):
    """Serve the cabinet every 15 minutes whatever IDLE saw, until the cabinet is deactivated, so an email IDLE missed
    (connection down, server quirk) or a thread whose run failed does not wait longer."""
    with using(cabinet):
        while cabinet in CABINETS:
            time.sleep(TICK_SECONDS)
            serve(cabinet, "")


def mailboxes():
    """Return the active cabinets with a Tarik mailbox of their own (IMAP_USER set), sorted."""
    found = []
    for cabinet in sorted(CABINETS):
        with using(cabinet):
            if setting("IMAP_USER", ""):
                found.append(cabinet)
    return found


def start(loop):
    """Remember the server's event ``loop`` (where Emails runs start) and start watching every cabinet's mailbox, in
    background threads."""
    global LOOP
    LOOP = loop
    watched = mailboxes()
    log(f"mailboxes: {', '.join(watched) or 'none'}")
    for cabinet in watched:
        start_one(cabinet)


def start_one(cabinet):
    """Start watching ``cabinet``'s mailbox; return False when it has no IMAP_USER, else True.

    Starts the watch and tick threads that are not already running (RUNNING). A thread still running after a
    deactivation and reactivation is kept, not doubled."""
    with using(cabinet):
        if not setting("IMAP_USER", ""):
            return False
    for job in (watch, tick):
        if (cabinet, job.__name__) not in RUNNING:
            RUNNING.add((cabinet, job.__name__))
            threading.Thread(target=alive, args=(job, cabinet), daemon=True).start()
    return True


def alive(job, cabinet):
    """Run ``job(cabinet)`` (watch or tick) in this thread, and remove it from RUNNING when it ends."""
    try:
        job(cabinet)
    finally:
        RUNNING.discard((cabinet, job.__name__))
