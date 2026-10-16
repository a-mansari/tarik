"""Tarik's mailbox over IMAP, read without changing it (folder opened read-only, bodies
fetched with BODY.PEEK): no flag is set, nothing is moved. The one thing Tarik adds is a
copy of each email it sends, in the Sent folder (tarik.emails.sending.save_sent). What Tarik did with each email
is tracked in Postgres instead, thread by thread, so an email someone already read in the
mailbox is still answered, and a thread waiting for the client comes back when they reply.

Only the cabinet's collaborators in Experio are answered, and only if the whitelist
(MAIL_ALLOWED_RECIPIENTS) lets Tarik write to them: any other email is set aside unread by
Tarik, so it creates nothing in Experio.
"""

import hashlib
import re
from email import message_from_bytes, policy
from email.utils import parseaddr

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server
from tarik.dossiers.people import collaborator
from tarik.core.cabinets import cabinet_db, current, setting
from tarik.core.experio import odoo_call
from tarik.emails.sending import imap_login, recipient_allowed, send_email

LEASE_MINUTES = 30  # a run longer than this is taken as dead: the thread is picked up again
MAX_ATTEMPTS = 3
FILE_TYPES = (".pdf", ".jpg", ".jpeg", ".png", ".csv", ".txt", ".xlsx", ".xlsm")
# Where the client's own text stops and the quoted history begins.
QUOTE_START = re.compile(r"^\s*(le .{0,120} a [ée]crit\s*:|on .{0,120} wrote\s*:|-{2,}\s*(message d'origine|original message)"
                         r"|de\s*:\s|from\s*:\s|envoy[ée]\s*:\s)", re.IGNORECASE)
def folder():
    """The current cabinet's mailbox folder (IMAP_FOLDER)."""
    return setting("IMAP_FOLDER", "INBOX")


def own_addresses():
    """The current cabinet's own addresses: an email from one of them is Tarik's own."""
    return {address.lower() for address in (parseaddr(setting("SMTP_FROM", ""))[1], setting("SMTP_USER", ""),
                                            setting("IMAP_USER", "")) if address}


def connect():
    """The folder, opened read-only, on the pinned (or verified) certificate."""
    box = imap_login()
    box.select(f'"{folder()}"', readonly=True)
    return box


def client_text(message):
    """The client's own words: the plain text (or the HTML without its tags), the quoted history cut off."""
    part = message.get_body(preferencelist=("plain", "html"))
    text = part.get_content() if part else ""
    if part and part.get_content_type() == "text/html":
        text = re.sub(r"<(br|/p|/div)[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", re.sub(r"<(style|script)[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL))
    lines = []
    for line in text.splitlines():
        if QUOTE_START.match(line):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:4000]


def attachments(message):
    """The files the client sent: an invoice, a statement, a FEC. Inline images (signature logos) are left out."""
    found = []
    for part in message.iter_attachments():
        name = part.get_filename() or ""
        if not name.lower().endswith(FILE_TYPES):
            continue
        if part.get_content_maintype() == "image" and part.get_content_disposition() == "inline":
            continue
        found.append((name, part.get_content_type(), part.get_payload(decode=True) or b""))
    return found


def automatic(message):
    """Out-of-office replies, bounces and mailing lists: never answered (a reply loop otherwise)."""
    sender = parseaddr(message.get("From", ""))[1].lower()
    return (message.get("Auto-Submitted", "no").lower() != "no" or message.get("Precedence", "").lower() in ("bulk", "list", "junk")
            or bool(message.get("X-Autoreply") or message.get("List-Id"))
            or re.match(r"(no-?reply|mailer-daemon|postmaster)@", sender) is not None)


def session_of(thread_id):
    """The thread's conversation id, the cabinet's own: n8n's conversation ids are shared by every agent,
    and one email may reach two cabinets' mailboxes."""
    return "mail-" + hashlib.sha256(f"{current()}\n{thread_id}".encode()).hexdigest()[:20]


class UnreadableEmail(Exception):
    """An email Python can't read (an unknown charset, a broken header): skipped, never tried again."""


def store(db, uid, message):
    """Record one email in its sender's thread (found by In-Reply-To and References), with its files.
    Who wrote is the From address alone: Reply-To is only where the reply may go. A reply from someone
    else in a thread (a colleague in copy) starts that person's own thread, with their own rights; a
    refused one is kept aside and leaves the thread alone. Returns what the cabinet's manager must hear
    of when the sender may not use Tarik, else None."""
    message_id = (message.get("Message-ID") or f"<uid-{uid}@{folder()}>").strip()
    if db.execute("select 1 from tarik_mail_messages where message_id = %s", (message_id,)).fetchone():
        return
    try:
        sender = parseaddr(message.get("From", ""))[1].lower()
        reply_to = parseaddr(message.get("Reply-To", ""))[1].lower() or sender
        subject = str(message.get("Subject", "") or "").strip()
        linked = re.findall(r"<[^>]+>", f"{message.get('In-Reply-To', '')} {message.get('References', '')}")
        text, files, auto = client_text(message), attachments(message), automatic(message)
        # Postgres text never holds a NUL: one in an email would stop every reading at it.
        sender, reply_to, subject, text = (value.replace("\x00", "") for value in (sender, reply_to, subject, text))
        files = [(name.replace("\x00", ""), mime, data) for name, mime, data in files]
    except Exception as error:
        raise UnreadableEmail(f"{message_id}: {type(error).__name__}: {error}") from None
    if sender in own_addresses():
        return  # Tarik's own reply, when the folder holds the sent copies
    if auto:
        reason, unauthorized = "reponse automatique ou liste de diffusion", None
    elif not recipient_allowed(sender):
        reason = unauthorized = "expediteur hors de la liste autorisee"
    elif not collaborator(sender):
        reason = unauthorized = "expediteur hors des collaborateurs du cabinet dans Experio"
    else:
        reason = unauthorized = None
    row = db.execute("select m.thread_id from tarik_mail_messages m join tarik_mail_threads t on t.thread_id = m.thread_id "
                     "where m.message_id = any(%s) and t.client_address = %s limit 1", (linked, sender)).fetchone()
    thread_id = row[0] if row else message_id
    if not row:
        db.execute("insert into tarik_mail_threads (thread_id, session_id, client_address, subject, state, reason) "
                   "values (%s, %s, %s, %s, %s, %s)", (thread_id, session_of(thread_id), sender, subject[:300],
                                                       "ignored" if reason else "new", reason))
    elif not reason:  # the client wrote again, allowed now: back to Tarik, even after an error or a refusal (a run on it takes it after)
        db.execute("update tarik_mail_threads set state = 'new', attempts = 0, reason = null, updated_at = now() "
                   "where thread_id = %s and state <> 'in_progress'", (thread_id,))
    session = db.execute("select session_id from tarik_mail_threads where thread_id = %s", (thread_id,)).fetchone()[0]
    db.execute("insert into tarik_mail_messages (message_id, thread_id, uid, from_address, reply_to, subject, body, references_ids, "
               "handled) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
               (message_id, thread_id, uid, sender, reply_to, subject[:300], text, " ".join(linked), reason is not None))
    if not reason:
        for name, mime, data in files:
            db.execute("insert into tarik_files (session_id, file_name, mime_type, data) values (%s, %s, %s, %s)",
                       (session, name, mime, data))
    return {"sender": sender, "subject": subject, "reason": unauthorized} if unauthorized else None


def claim(db):
    """The threads with emails Tarik hasn't answered, locked for this run; after 3 failed runs, set aside."""
    db.execute("update tarik_mail_threads set state = 'error', reason = 'traitement echoue 3 fois', claimed_at = null "
               "where state = 'in_progress' and attempts >= %s and claimed_at < now() - make_interval(mins => %s)",
               (MAX_ATTEMPTS, LEASE_MINUTES))
    rows = db.execute("update tarik_mail_threads set state = 'in_progress', claimed_at = now(), attempts = attempts + 1, "
                      "updated_at = now() where state = 'new' or (state = 'in_progress' and claimed_at < now() - make_interval(mins => %s)) "
                      "returning thread_id, session_id, client_address, subject", (LEASE_MINUTES,)).fetchall()
    threads = []
    for thread_id, session_id, address, subject in rows:
        messages = db.execute("select message_id, body, references_ids, reply_to from tarik_mail_messages "
                              "where thread_id = %s and not handled order by received_at, uid", (thread_id,)).fetchall()
        files = [name for (name,) in db.execute(
            "select file_name from tarik_files where session_id = %s and parent_id is null and kind is null "
            "and invoice_id is null and statement_id is null order by id", (session_id,))]
        last_id, _, last_references, reply_to = messages[-1] if messages else ("", "", "", None)
        threads.append({"thread_id": thread_id, "session_id": session_id,
                        "to": reply_to if reply_to and recipient_allowed(reply_to) else address,
                        "subject": subject if subject.lower().startswith(("re:", "re :")) else f"Re: {subject}",
                        "message": "\n\n".join(body for _, body, _, _ in messages if body) or "(pas de texte)",
                        "files": files, "message_ids": [message_id for message_id, *_ in messages],
                        "in_reply_to": last_id, "references": last_references})
    return threads


def read_mailbox():
    """Store the mailbox's new emails in their threads (without changing anything there), and return
    what the cabinet's manager must hear of: the refused senders. Run by tarik-mail-watch, never by n8n:
    however many emails wait, no workflow waits on it. The first reading only notes where the mailbox
    stands: earlier emails are left alone."""
    box = connect()
    try:
        uidvalidity = int(box.response("UIDVALIDITY")[1][0])
        newest = int(box.response("UIDNEXT")[1][0]) - 1
        with cabinet_db() as db:
            # One reading at a time per cabinet (the watcher's wake-up and its tick may overlap): held until this
            # connection closes, across the commits below.
            if not db.execute("select pg_try_advisory_lock(hashtext(%s))", (f"read_mailbox {current()}",)).fetchone()[0]:
                return []
            state = db.execute("select uidvalidity, last_uid from tarik_mail_state where folder = %s", (folder(),)).fetchone()
            if not state or state[0] != uidvalidity:  # first reading, or the folder was rebuilt: start from now
                db.execute("insert into tarik_mail_state (folder, uidvalidity, last_uid) values (%s, %s, %s) on conflict (folder) "
                           "do update set uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid", (folder(), uidvalidity, newest))
                return []
            uids = [int(uid) for uid in box.uid("search", None, f"UID {state[1] + 1}:*")[1][0].split()]
            alerts = []
            for uid in sorted(u for u in uids if u > state[1]):  # "n:*" also returns the last email when there's nothing new
                data = box.uid("fetch", str(uid), "(BODY.PEEK[])")[1]
                raw = next((part[1] for part in data if isinstance(part, tuple)), None)
                try:
                    with db.transaction():  # a failed email leaves nothing half-written
                        alert = store(db, uid, message_from_bytes(raw, policy=policy.default)) if raw else None
                except (UnreadableEmail, psycopg.DataError) as error:  # this email can't be stored: never tried again
                    print(f"read_mailbox: unreadable email skipped: {error}", flush=True)
                    alert = None
                except Exception as error:  # Experio or the database out: this email and the next wait for the next reading
                    print(f"read_mailbox: stopped at uid {uid}: {type(error).__name__}: {error}", flush=True)
                    break
                if alert:
                    alerts.append(alert)
                db.execute("update tarik_mail_state set last_uid = %s where folder = %s", (uid, folder()))
                db.commit()  # each email recorded for good before the next
    finally:
        box.logout()
    return alerts


def threads_waiting():
    """Is a thread waiting for Tarik's answer, so that "Tarik - Emails" has work?"""
    with cabinet_db() as db:
        return db.execute("select 1 from tarik_mail_threads where state = 'new' or (state = 'in_progress' and "
                          "claimed_at < now() - make_interval(mins => %s)) limit 1", (LEASE_MINUTES,)).fetchone() is not None


@server.tool()
def claim_threads() -> dict:
    """The threads waiting for Tarik's answer, each locked for this run (tarik-mail-watch stored their emails)."""
    with cabinet_db() as db:
        threads = claim(db)
    return ok(f"{len(threads)} fil(s) a traiter.", threads=threads)


@server.tool()
def thread_refusal(thread_id: str) -> dict:
    """After Tarik's run on a thread: was the work refused because the sender lacks the
    dossier (check_sender)? Then nothing goes back to the sender: the thread is set aside,
    its emails counted as handled, and ``refused`` says the manager is to be told."""
    with cabinet_db() as db:
        thread = db.execute("select session_id, client_address, subject, claimed_at from tarik_mail_threads where thread_id = %s",
                            (thread_id,)).fetchone()
        if not thread:
            return ok("Fil inconnu.", refused=False)
        session_id, sender, subject, claimed_at = thread
        refusal = db.execute("select message from tarik_results where session_id = %s and step = 'check_sender' and status = 'stop' "
                             "and created_at >= %s order by id desc limit 1", (session_id, claimed_at)).fetchone()
        if not refusal:
            return ok("Pas de refus.", refused=False)
        db.execute("update tarik_mail_messages set handled = true where thread_id = %s", (thread_id,))
        db.execute("update tarik_mail_threads set state = 'ignored', reason = %s, claimed_at = null, updated_at = now() "
                   "where thread_id = %s", (refusal[0][:300], thread_id))
    return ok("Travail refuse : l'expediteur n'a pas ce dossier.", refused=True, sender=sender, subject=subject, reason=refusal[0])


def cabinet_manager():
    """The cabinet's manager: the "Responsable" of the cabinet's own company in Experio, else
    MAIL_MANAGER. Only an active collaborator counts. Experio has no other place for it."""
    responsable = odoo_call("res.company", "read", [1], fields=["responsable_id"])[0]["responsable_id"]
    if responsable:
        found = odoo_call("res.users", "read", [responsable[0]], fields=["login"])
        if found and collaborator(found[0]["login"]):
            return found[0]["login"]
    fallback = setting("MAIL_MANAGER", "").strip()
    return fallback if fallback and collaborator(fallback) else None


@server.tool()
def notify_manager(sender: str, subject: str, reason: str) -> dict:
    """Tell the cabinet's manager that ``sender`` wrote to Tarik and was refused, at most
    once a day per sender. The sender gets nothing."""
    manager = cabinet_manager()
    if not manager:
        raise ToolError("Manager non prevenu : aucun Responsable sur la societe du cabinet dans Experio, et MAIL_MANAGER "
                        "n'est pas l'email d'un collaborateur du cabinet.")
    with cabinet_db() as db:
        if db.execute("select 1 from tarik_mail_alerts where sender = %s and alerted_at > now() - interval '1 day'",
                      (sender,)).fetchone():
            return ok(f"Manager deja prevenu aujourd'hui pour {sender}.")
        send_email(manager, f"Tarik : email de {sender} non traite",
                   f"Bonjour,\n\n{sender} a ecrit a Tarik (objet : « {subject or 'sans objet'} »).\n"
                   f"Son email n'a pas ete traite : {reason}.\n\nL'expediteur n'a recu aucune reponse.")
        db.execute("insert into tarik_mail_alerts (sender) values (%s)", (sender,))
    return ok(f"Manager prevenu : {sender}.")


@server.tool()
def finish_thread(thread_id: str, message_ids: list, sent: bool, reason: str = "") -> dict:
    """After Tarik's reply: its emails are answered, and the thread waits for the client
    when Tarik asked a question. When the reply could not be sent, the thread is set
    aside in error, with ``reason``."""
    with cabinet_db() as db:
        session = db.execute("select session_id from tarik_mail_threads where thread_id = %s", (thread_id,)).fetchone()
        if not session:
            return ok("Fil inconnu.")
        if not sent:
            db.execute("update tarik_mail_threads set state = 'error', reason = %s, claimed_at = null, updated_at = now() "
                       "where thread_id = %s", ((reason or "reponse non envoyee")[:300], thread_id))
            return ok("Fil mis de cote : reponse non envoyee.")
        db.execute("update tarik_mail_messages set handled = true where message_id = any(%s)", (list(message_ids),))
        newer = db.execute("select 1 from tarik_mail_messages where thread_id = %s and not handled limit 1", (thread_id,)).fetchone()
        waiting = db.execute("select 1 from tarik_pending where session_id = %s limit 1", session).fetchone()
        state = "new" if newer else "waiting" if waiting else "done"  # new: an email came in during this run
        db.execute("update tarik_mail_threads set state = %s, claimed_at = null, attempts = 0, reason = null, updated_at = now() "
                   "where thread_id = %s", (state, thread_id))
    return ok({"new": "Fil traite, un nouvel email attend.", "waiting": "Fil en attente du client.", "done": "Fil traite."}[state])
