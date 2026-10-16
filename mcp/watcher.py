"""Read each Tarik mailbox (a cabinet's own IMAP_USER) the moment an email arrives, and every 15 minutes in
case one was missed: store the new emails in their threads, tell the cabinet's manager of refused senders,
and wake "Tarik - Emails" when a thread waits for an answer. Each wake-up names its cabinet, and the workflow
works in that cabinet only. Run as: python watcher.py (a new mailbox needs a restart).

Per cabinet, one IMAP IDLE connection to its Tarik folder (read-only, pinned certificate, as
read_mailbox connects): the server tells it when the folder gets a new email, and it reads it. The
connection is renewed every 25 minutes (servers drop an IDLE after ~30) and rebuilt after any error;
the 15-minute tick still catches whatever happens meanwhile.
"""

import logging
import os
import threading
import time

import httpx

from tarik.core.app import log
from tarik.core.cabinets import CABINETS, setting, using
from tarik.emails.inbox import connect, notify_manager, read_mailbox, threads_waiting

WEBHOOK = os.getenv("MAIL_WEBHOOK_URL", "http://n8n:5678/webhook/tarik-nouvel-email")
RENEW_SECONDS = 25 * 60
TICK_SECONDS = 15 * 60
HEADER = "X-Tarik-Webhook"  # the webhook's secret (Vault MAIL_WEBHOOK_SECRET; n8n credential "Tarik mail webhook")

HTTP = httpx.Client()
logging.getLogger("httpx").setLevel(logging.WARNING)  # not one line per wake-up


def wait_for_mail(box, count, seconds):
    """IDLE on the folder holding ``count`` emails until one more arrives, or ``seconds`` pass: (arrived, the
    folder's count)."""
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
    """Call "Tarik - Emails"' webhook for ``cabinet``, logging ``why`` (nothing when ""). A failure is only logged."""
    try:
        HTTP.post(WEBHOOK, json={"cabinet": cabinet}, timeout=10, headers={HEADER: setting("MAIL_WEBHOOK_SECRET")})
        if why:
            log(f"{cabinet}: {why}, Tarik - Emails woken")
    except Exception as error:  # the next tick tries again
        log(f"{cabinet}: webhook failed: {type(error).__name__}")


def serve(cabinet, why):
    """Store the cabinet's new emails, tell its manager of the refused senders, and wake the workflow when a thread
    waits. Whatever fails is logged: the next wake-up or tick tries again."""
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
    """IDLE on the cabinet's mailbox: Tarik is woken the moment an email arrives."""
    with using(cabinet):
        while True:
            try:
                with connect() as box:
                    count = int(box.untagged_responses.get("EXISTS", [b"0"])[-1])
                    log(f"{cabinet}: watching ({count} emails)")
                    while True:
                        arrived, count = wait_for_mail(box, count, RENEW_SECONDS)
                        if arrived:
                            serve(cabinet, "new email")
            except Exception as error:
                log(f"{cabinet}: watch interrupted: {error}; reconnecting in 30 s")
                time.sleep(30)


def tick(cabinet):
    """Every 15 minutes, whatever IDLE saw: an email it missed (connection down, server quirk), or a thread whose
    run failed, waits no longer."""
    with using(cabinet):
        while True:
            time.sleep(TICK_SECONDS)
            serve(cabinet, "")


def mailboxes():
    """The cabinets that have a Tarik mailbox: their own IMAP_USER."""
    found = []
    for cabinet in sorted(CABINETS):
        with using(cabinet):
            if setting("IMAP_USER", ""):
                found.append(cabinet)
    return found


if __name__ == "__main__":
    watched = mailboxes()
    log(f"mailboxes: {', '.join(watched) or 'none'}")
    threads = [
        threading.Thread(target=job, args=(cabinet,), daemon=True) for cabinet in watched for job in (watch, tick)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
