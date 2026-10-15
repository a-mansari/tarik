"""Wake "Tarik - Emails" for each cabinet with a Tarik mailbox (its own IMAP_USER): the moment an
email arrives, and every 2 minutes in case one was missed. Each wake-up names its cabinet, and the
workflow works in that cabinet only. Run as: python watcher.py (a new mailbox needs a restart).

Per cabinet, one IMAP IDLE connection to its Tarik folder (read-only, pinned certificate, as
fetch_emails connects): the server tells it when the folder gets a new email, and it calls the
workflow's webhook. The connection is renewed every 25 minutes (servers drop an IDLE after ~30)
and rebuilt after any error; the 2-minute tick still catches whatever happens meanwhile.
Python 3.12's imaplib has no IDLE (3.14 does): the command is spoken here.
"""

import logging
import os
import re
import select
import threading
import time

import httpx

from tarik.core.cabinets import CABINETS, setting, using
from tarik.emails.inbox import connect

WEBHOOK = os.getenv("MAIL_WEBHOOK_URL", "http://n8n:5678/webhook/tarik-nouvel-email")
RENEW_SECONDS = 25 * 60
TICK_SECONDS = 2 * 60
HEADER = "X-Tarik-Webhook"  # the webhook's secret (Vault MAIL_WEBHOOK_SECRET; n8n credential "Tarik mail webhook")


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
        found = re.match(rb"\* (\d+) (EXISTS|EXPUNGE)", line)
        if found and found.group(2) == b"EXISTS":
            grown = int(found.group(1))
        elif found:  # an email deleted elsewhere: the count to beat goes down with it
            count, grown = count - 1, grown - 1
    box.send(b"DONE\r\n")
    while not box.readline().startswith(tag.encode()):  # the rest of the IDLE's answers, up to its end
        pass
    return grown


def wake_tarik(cabinet, why):
    try:
        HTTP.post(WEBHOOK, json={"cabinet": cabinet}, timeout=10, headers={HEADER: setting("MAIL_WEBHOOK_SECRET")})
        if why:
            print(f"{cabinet}: {why}, Tarik - Emails woken", flush=True)
    except Exception as error:  # the next tick tries again
        print(f"{cabinet}: webhook failed: {type(error).__name__}", flush=True)


def watch(cabinet):
    """IDLE on the cabinet's mailbox: Tarik is woken the moment an email arrives."""
    with using(cabinet):
        while True:
            try:
                box = connect()
                count = int(box.untagged_responses.get("EXISTS", [b"0"])[-1])
                print(f"{cabinet}: watching ({count} emails)", flush=True)
                while True:
                    grown = wait_for_mail(box, count, RENEW_SECONDS)
                    if grown > count:
                        wake_tarik(cabinet, "new email")
                    count = grown
            except Exception as error:
                print(f"{cabinet}: watch interrupted: {error}; reconnecting in 30 s", flush=True)
                time.sleep(30)


def tick(cabinet):
    """Every 2 minutes, whatever IDLE saw: an email it missed (connection down, server quirk) waits no longer."""
    with using(cabinet):
        while True:
            time.sleep(TICK_SECONDS)
            wake_tarik(cabinet, "")


def mailboxes():
    """The cabinets that have a Tarik mailbox: their own IMAP_USER."""
    found = []
    for cabinet in sorted(CABINETS):
        with using(cabinet):
            if setting("IMAP_USER", ""):
                found.append(cabinet)
    return found


HTTP = httpx.Client()
logging.getLogger("httpx").setLevel(logging.WARNING)  # not one line per wake-up

if __name__ == "__main__":
    watched = mailboxes()
    print(f"mailboxes: {', '.join(watched) or 'none'}", flush=True)
    threads = [threading.Thread(target=job, args=(cabinet,), daemon=True) for cabinet in watched for job in (watch, tick)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
