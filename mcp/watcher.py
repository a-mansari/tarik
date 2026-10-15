"""Wake "Tarik - Emails" the moment an email arrives, instead of at its next 2-minute check.

One IMAP IDLE connection to Tarik's folder (read-only, pinned certificate, as fetch_emails
connects): the server tells it when the folder gets a new email, and it calls the workflow's
webhook. The connection is renewed every 25 minutes (servers drop an IDLE after ~30) and
rebuilt after any error; the 2-minute schedule still catches whatever happens meanwhile.
Python 3.12's imaplib has no IDLE (3.14 does): the command is spoken here.
"""

import os
import re
import select
import time

import httpx

from tarik.emails.inbox import connect

WEBHOOK = os.getenv("MAIL_WEBHOOK_URL", "http://n8n:5678/webhook/tarik-nouvel-email")
RENEW_SECONDS = 25 * 60


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


def wake_tarik():
    try:
        HTTP.post(WEBHOOK, json={}, timeout=10)
        print("new email: Tarik - Emails woken", flush=True)
    except Exception as error:  # the 2-minute schedule takes it anyway
        print(f"webhook failed: {error}", flush=True)


def watch():
    while True:
        try:
            box = connect()
            count = int(box.untagged_responses.get("EXISTS", [b"0"])[-1])
            print(f"watching ({count} emails)", flush=True)
            while True:
                grown = wait_for_mail(box, count, RENEW_SECONDS)
                if grown > count:
                    wake_tarik()
                count = grown
        except Exception as error:
            print(f"watch interrupted: {error}; reconnecting in 30 s", flush=True)
            time.sleep(30)


HTTP = httpx.Client()

if __name__ == "__main__":
    watch()
