"""Read each Tarik mailbox (a cabinet's own IMAP_USER) the moment an email arrives, and every 2 minutes in
case one was missed: store the new emails in their threads, tell the cabinet's manager of refused senders,
and wake "Tarik - Emails" when a thread waits for an answer. Each wake-up names its cabinet, and the workflow
works in that cabinet only. Run as: python watcher.py (a new mailbox needs a restart).

Per cabinet, one IMAP IDLE connection to its Tarik folder (read-only, pinned certificate, as
read_mailbox connects): the server tells it when the folder gets a new email, and it reads it. The
connection is renewed every 25 minutes (servers drop an IDLE after ~30) and rebuilt after any error;
the 2-minute tick still catches whatever happens meanwhile.
Python 3.12's imaplib has no IDLE (3.14 does): the command is spoken here.
"""

import logging
import os
import re
import select
import threading
import time

import httpx

from tarik.core.app import log
from tarik.core.cabinets import CABINETS, setting, using
from tarik.emails.inbox import connect, notify_manager, read_mailbox, threads_waiting

WEBHOOK = os.getenv("MAIL_WEBHOOK_URL", "http://n8n:5678/webhook/tarik-nouvel-email")
RENEW_SECONDS = 25 * 60
TICK_SECONDS = 2 * 60
HEADER = "X-Tarik-Webhook"  # the webhook's secret (Vault MAIL_WEBHOOK_SECRET; n8n credential "Tarik mail webhook")
# The server's count of emails in the folder: "* <n> EXISTS" (one more arrived) or "* <n> EXPUNGE" (one deleted).
COUNT = re.compile(rb"\* (\d+) (EXISTS|EXPUNGE)")

HTTP = httpx.Client()
logging.getLogger("httpx").setLevel(logging.WARNING)  # not one line per wake-up


def wait_for_mail(box, count, seconds):
    """IDLE until the folder holds more than ``count`` emails, or ``seconds`` pass. Returns the new count."""
    tag = box._new_tag().decode()
    box.send(f"{tag} IDLE\r\n".encode())
    if not box.readline().startswith(b"+"):
        raise ConnectionError("IDLE refused")
    deadline, grown = time.monotonic() + seconds, count
    while time.monotonic() < deadline and grown <= count:
        # Data TLS already decrypted is invisible to select: read it first.
        if not box.sock.pending() and not select.select([box.sock], [], [], min(30, deadline - time.monotonic()))[0]:
            continue
        line = box.readline()
        if not line:
            raise ConnectionError("connection closed")
        found = COUNT.match(line)
        if found and found.group(2) == b"EXISTS":
            grown = int(found.group(1))
        elif found:  # an email deleted elsewhere: the count to beat goes down with it
            count, grown = count - 1, grown - 1
    box.send(b"DONE\r\n")
    while True:  # the rest of the IDLE's answers, up to its end
        line = box.readline()
        if not line:
            raise ConnectionError("connection closed")
        if line.startswith(tag.encode()):
            return grown


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
                box = connect()
                count = int(box.untagged_responses.get("EXISTS", [b"0"])[-1])
                log(f"{cabinet}: watching ({count} emails)")
                while True:
                    grown = wait_for_mail(box, count, RENEW_SECONDS)
                    if grown > count:
                        serve(cabinet, "new email")
                    count = grown
            except Exception as error:
                log(f"{cabinet}: watch interrupted: {error}; reconnecting in 30 s")
                time.sleep(30)


def tick(cabinet):
    """Every 2 minutes, whatever IDLE saw: an email it missed (connection down, server quirk), or a thread whose
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
