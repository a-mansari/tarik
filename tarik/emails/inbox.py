"""Tarik's mailbox over IMAP, read without changing it (folder opened read-only, bodies
fetched with BODY.PEEK): no flag is set, nothing is moved. The one thing Tarik adds is a
copy of each email it sends, in the Sent folder (tarik.emails.sending.save_sent). What Tarik did with each email
is tracked in Postgres instead, thread by thread, so an email someone already read in the
mailbox is still answered, and a thread waiting for the client comes back when they reply.

Only the cabinet's collaborators and Experio's own staff are answered, and only if the whitelist
(MAIL_ALLOWED_RECIPIENTS) lets Tarik write to them: any other email is set aside unread by
Tarik, so it creates nothing in Experio.
"""

import contextlib
import hashlib
import re
from email import message_from_bytes, policy
from email.utils import parseaddr

import psycopg

from tarik.core.app import ToolError, log, ok, tool
from tarik.core.cabinets import cabinet_db, current, setting
from tarik.core.config import ENTRY_FILES, READING
from tarik.core.experio import odoo_call
from tarik.core.queries import Q
from tarik.core.words import strip_tags
from tarik.dossiers.people import collaborator
from tarik.dossiers.setup import waiting_setups
from tarik.emails.sending import imap_session, recipient_allowed, send_email
from tarik.invoices.pipeline import waiting_drafts

LEASE_MINUTES = 30  # a run longer than this is taken as dead: the thread is picked up again
MAX_ATTEMPTS = 3
FOLLOW_UP_MINUTES = 30  # how long after Tarik's reply the drafts Experio was reading are still followed up
# The acknowledgement of a new thread, sent before the agent works on it (vouvoiement, no civility, as Tarik writes).
ACKNOWLEDGEMENT = (
    "Votre message est bien reçu. Je le traite et vous réponds dans ce fil, en général d'ici quelques minutes."
)
FILE_TYPES = (".pdf", ".jpg", ".jpeg", ".png", *ENTRY_FILES)

# Where the client's own text stops and the quoted history begins: a line starting with "Le ... a écrit :",
# "On ... wrote:", "-- Message d'origine" / "-- Original message", or a quoted header ("De :", "From:", "Envoyé :").
QUOTE_START = re.compile(
    r"^\s*(le .{0,120} a [ée]crit\s*:|on .{0,120} wrote\s*:|-{2,}\s*(message d'origine|original message)"
    r"|de\s*:\s|from\s*:\s|envoy[ée]\s*:\s)",
    re.IGNORECASE,
)
LINE_END_TAG = re.compile(r"<(br|/p|/div)[^>]*>", re.IGNORECASE)  # <br>, </p> or </div>: where an HTML line ends
HIDDEN = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)  # a style or script element
BLANK_LINES = re.compile(r"\n{3,}")  # more than one blank line in a row
AUTOMATIC_SENDER = re.compile(r"(no-?reply|mailer-daemon|postmaster)@")  # noreply@, no-reply@, mailer-daemon@...
MESSAGE_ID = re.compile(r"<[^>]+>")  # one <message id> in In-Reply-To or References


def folder():
    """The current cabinet's mailbox folder (IMAP_FOLDER)."""
    return setting("IMAP_FOLDER", "INBOX")


@contextlib.contextmanager
def connect():
    """The folder, opened read-only, on the pinned (or verified) certificate; closed on the way out."""
    with imap_session() as box:
        box.select(f'"{folder()}"', readonly=True)
        yield box


def client_text(message):
    """The client's own words: the plain text (or the HTML without its tags), the quoted history cut off."""
    part = message.get_body(preferencelist=("plain", "html"))
    text = part.get_content() if part else ""
    if part and part.get_content_type() == "text/html":
        text = strip_tags(HIDDEN.sub("", LINE_END_TAG.sub("\n", text)))
    lines = []
    for line in text.splitlines():
        if QUOTE_START.match(line):
            break
        if not line.lstrip().startswith(">"):  # a quoted line
            lines.append(line.rstrip())
    return BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()[:4000]


def attachments(message):
    """The files the client sent, as [(name, mime type, bytes)]: an invoice, a statement, a FEC. Inline images
    (signature logos) are left out."""
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
    return (
        message.get("Auto-Submitted", "no").lower() != "no"
        or message.get("Precedence", "").lower() in ("bulk", "list", "junk")
        or bool(message.get("X-Autoreply") or message.get("List-Id"))
        or AUTOMATIC_SENDER.match(sender) is not None
    )


class UnreadableEmail(Exception):
    """An email Python can't read (an unknown charset, a broken header): skipped, never tried again."""


def read_email(message_id, message):
    """What ``message`` holds: (sender, reply_to, subject, linked message ids, text, files, automatic?).
    UnreadableEmail when Python can't read it."""
    try:
        sender = parseaddr(message.get("From", ""))[1].lower()
        reply_to = parseaddr(message.get("Reply-To", ""))[1].lower() or sender
        subject = str(message.get("Subject", "") or "").strip()
        linked = MESSAGE_ID.findall(f"{message.get('In-Reply-To', '')} {message.get('References', '')}")
        text, files, auto = client_text(message), attachments(message), automatic(message)
        # Postgres text never holds a NUL: one in an email would stop every reading at it.
        sender, reply_to, subject, text = (value.replace("\x00", "") for value in (sender, reply_to, subject, text))
        files = [(name.replace("\x00", ""), mime, data) for name, mime, data in files]
    except Exception as error:
        raise UnreadableEmail(f"{message_id}: {type(error).__name__}: {error}") from None
    return sender, reply_to, subject, linked, text, files, auto


def refusal(sender, auto):
    """Why an email from ``sender`` is not for Tarik, and why the manager must hear of it: (reason, unauthorized),
    each None when it does not apply."""
    if auto:
        return "reponse automatique ou liste de diffusion", None
    if not recipient_allowed(sender):
        return "expediteur hors de la liste autorisee", "expediteur hors de la liste autorisee"
    if not collaborator(sender):
        reason = "expediteur ni collaborateur du cabinet ni de l'equipe Experio"
        return reason, reason
    return None, None


def thread_of(db, linked, sender, subject, reason, message_id):
    """The id of the sender's thread the email belongs to (one of ``linked`` is in it); a new thread, ignored
    when there is a ``reason``, otherwise. A thread the client wrote again in goes back to Tarik."""
    row = db.execute(
        Q.thread_of_linked_message,
        (linked, sender),
    ).fetchone()
    if not row:
        # The thread's conversation, the cabinet's own: conversation ids are shared by every cabinet, and one
        # email may reach two cabinets' mailboxes.
        session_id = "mail-" + hashlib.sha256(f"{current()}\n{message_id}".encode()).hexdigest()[:20]
        db.execute(
            Q.insert_mail_thread,
            (message_id, session_id, sender, subject[:300], "ignored" if reason else "new", reason),
        )
        return message_id
    if not reason:  # allowed now: back to Tarik, even after an error or a refusal (a run on it takes it after)
        db.execute(
            Q.reopen_mail_thread,
            (row[0],),
        )
    return row[0]


def store(db, uid, message):
    """Record one email in its sender's thread (found by In-Reply-To and References), with its files.
    Who wrote is the From address alone: Reply-To is only where the reply may go. A reply from someone
    else in a thread (a colleague in copy) starts that person's own thread, with their own rights; a
    refused one is kept aside and leaves the thread alone. Returns what the cabinet's manager must hear
    of when the sender may not use Tarik, else None."""
    message_id = (message.get("Message-ID") or f"<uid-{uid}@{folder()}>").strip()
    if db.execute(Q.mail_message_exists, (message_id,)).fetchone():
        return
    sender, reply_to, subject, linked, text, files, auto = read_email(message_id, message)
    own = (parseaddr(setting("SMTP_FROM", ""))[1], setting("SMTP_USER", ""), setting("IMAP_USER", ""))
    if sender in {address.lower() for address in own if address}:
        return  # Tarik's own reply, when the folder holds the sent copies
    reason, unauthorized = refusal(sender, auto)
    thread_id = thread_of(db, linked, sender, subject, reason, message_id)
    session = db.execute(Q.session_of_thread, (thread_id,)).fetchone()[0]
    db.execute(
        Q.insert_mail_message,
        (message_id, thread_id, uid, sender, reply_to, subject[:300], text, " ".join(linked), reason is not None),
    )
    if not reason:
        for name, mime, data in files:
            db.execute(
                Q.insert_mail_file,
                (session, name, mime, data),
            )
    return {"sender": sender, "subject": subject, "reason": unauthorized} if unauthorized else None


def thread_item(db, thread_id, session_id, address, subject):
    """A claimed thread as the workflow works on it (dict): where the reply goes, its subject, the client's
    unanswered emails as one message, the files waiting, and what the reply answers (the thread's last email).
    With no unanswered email, it is a follow-up: Tarik takes up the drafts Experio was reading."""
    messages = db.execute(
        Q.unhandled_messages_of_thread,
        (thread_id,),
    ).fetchall()
    last = db.execute(
        Q.last_message_of_thread,
        (thread_id,),
    ).fetchone()
    files = db.execute(
        Q.untaken_file_names,
        (session_id,),
    )
    last_id, last_references, reply_to = last or ("", "", None)
    return {
        "thread_id": thread_id,
        "session_id": session_id,
        "to": reply_to if reply_to and recipient_allowed(reply_to) else address,
        "subject": subject if subject.lower().startswith(("re:", "re :")) else f"Re: {subject}",
        "message": "\n\n".join(body for _, body in messages if body) or "(pas de texte)",
        "files": [name for (name,) in files],
        "message_ids": [message_id for message_id, _ in messages],
        "follow_up": not messages,
        "in_reply_to": last_id,
        "references": last_references,
    }


def claim(db):
    """The threads with emails Tarik hasn't answered (list of thread_item), locked for this run; after 3 failed
    runs, set aside."""
    db.execute(
        Q.set_aside_failed_threads,
        (f"traitement echoue {MAX_ATTEMPTS} fois", MAX_ATTEMPTS, LEASE_MINUTES),
    )
    ready = follow_ups_ready(db)
    rows = db.execute(
        Q.claim_mail_threads,
        (LEASE_MINUTES, ready),
    ).fetchall()
    return [thread_item(db, *row) for row in rows]


def drafts_to_resume(session_id):
    """The conversation's drafts to take up again (invoice ids); none when Experio can't be asked (logged)."""
    try:
        return waiting_drafts(session_id)["invoices"]
    except Exception as error:
        log(f"drafts_to_resume {session_id}: {type(error).__name__}: {error}")
        return []


def follow_ups_ready(db):
    """The follow-up threads to take now (thread ids): a dossier to finish, Experio has read every draft their
    conversation waits on, or their time is up. One with nothing left (drafts posted or deleted in Experio
    meanwhile) is done."""
    ready = []
    for thread_id, session_id, over in db.execute(Q.follow_up_threads).fetchall():
        if waiting_setups(session_id)["dossiers"]:  # Experio's wait on its lines is over by the next tick
            ready.append(thread_id)
            continue
        drafts = drafts_to_resume(session_id)
        if not drafts:
            db.execute(
                Q.finish_follow_up,
                (thread_id,),
            )
            continue
        try:
            read = odoo_call("exp.invoice", "read", drafts, fields=["bluejay_state"])
        except Exception as error:  # Experio out: asked again at the next tick
            log(f"follow_ups_ready {thread_id}: {type(error).__name__}: {error}")
            continue
        if over or all(draft["bluejay_state"] not in READING for draft in read):
            ready.append(thread_id)
    return ready


def last_read(db, uidvalidity, newest):
    """The uid of the last email read in the folder, or None on a first reading (or a folder rebuilt, its
    ``uidvalidity`` changed): then the folder's ``newest`` uid is noted, and earlier emails are left alone."""
    state = db.execute(Q.mail_folder_state, (folder(),)).fetchone()
    if state and state[0] == uidvalidity:
        return state[1]
    db.execute(
        Q.upsert_mail_folder_state,
        (folder(), uidvalidity, newest),
    )
    return None


def store_new(box, db, last):
    """Store each email after uid ``last``, each committed before the next; returns the manager's alerts. An
    unreadable email is skipped for good; any other failure stops here, for the next reading to go on."""
    uids = [int(uid) for uid in box.uid("search", None, f"UID {last + 1}:*")[1][0].split()]
    alerts = []
    for uid in sorted(u for u in uids if u > last):  # "n:*" also returns the last email when there's nothing new
        try:
            data = box.uid("fetch", str(uid), "(BODY.PEEK[])")[1]  # read without marking it read
            raw = next((part[1] for part in data if isinstance(part, tuple)), None)
            with db.transaction():  # a failed email leaves nothing half-written
                alert = store(db, uid, message_from_bytes(raw, policy=policy.default)) if raw else None
        except (UnreadableEmail, psycopg.DataError) as error:  # this email can't be stored: never tried again
            log(f"read_mailbox: unreadable email skipped: {error}")
            alert = None
        except Exception as error:  # Experio or the database out: this email and the next wait for the next reading
            log(f"read_mailbox: stopped at uid {uid}: {type(error).__name__}: {error}")
            break
        if alert:
            alerts.append(alert)
        db.execute(Q.set_mail_folder_last_uid, (uid, folder()))
        db.commit()
    return alerts


def read_mailbox():
    """Store the mailbox's new emails in their threads (without changing anything there), and return
    what the cabinet's manager must hear of: the refused senders. Run by the mail watcher (engine/mail.py), never by a
    workflow: however many emails wait, no run waits on it. The first reading only notes where the mailbox
    stands: earlier emails are left alone."""
    with connect() as box, cabinet_db() as db:
        uidvalidity = int(box.response("UIDVALIDITY")[1][0])
        newest = int(box.response("UIDNEXT")[1][0]) - 1
        # This cabinet's reading lock, held until db closes: the watcher's wake-up and its tick may overlap.
        if not db.execute(Q.try_mailbox_lock, (f"read_mailbox {current()}",)).fetchone()[0]:
            return []
        last = last_read(db, uidvalidity, newest)
        return [] if last is None else store_new(box, db, last)


def threads_waiting():
    """Is a thread waiting for Tarik's answer, so that "Tarik - Emails" has work?"""
    with cabinet_db() as db:
        waiting = db.execute(Q.any_thread_waiting, (LEASE_MINUTES,)).fetchone()
        return waiting is not None or bool(follow_ups_ready(db))


def set_aside(db, thread_id, state, reason):
    """Unlock thread ``thread_id`` in ``state`` ('ignored' or 'error'), with ``reason`` (str, cut to 300)."""
    db.execute(
        Q.set_aside_thread,
        (state, reason[:300], thread_id),
    )


@tool
def acknowledge_thread(thread_id: str) -> dict:
    """Tell the client their new thread was received, in that thread, before Tarik works on it: once per
    thread, and only for one Tarik never answered (a reply in an existing thread or a follow-up gets none)."""
    with cabinet_db() as db:
        row = db.execute(
            Q.thread_to_acknowledge,
            (thread_id,),
        ).fetchone()
        if not row or not row[3]:
            return ok("Pas d'accuse de reception : fil deja suivi.", sent=False)
        item = thread_item(db, thread_id, *row[:3])
    send_email(item["to"], item["subject"], ACKNOWLEDGEMENT, item["in_reply_to"], item["references"])
    with cabinet_db() as db:
        db.execute(Q.set_thread_acknowledged, (thread_id,))
    return ok("Accuse de reception envoye.", sent=True)


@tool
def claim_threads() -> dict:
    """The threads waiting for Tarik's answer, each locked for this run (tarik-mail-watch stored their emails)."""
    with cabinet_db() as db:
        threads = claim(db)
    return ok(f"{len(threads)} fil(s) a traiter.", threads=threads)


@tool
def thread_refusal(thread_id: str) -> dict:
    """After Tarik's run on a thread: was the work refused because the sender lacks the
    dossier (check_sender)? Then nothing goes back to the sender: the thread is set aside,
    its emails counted as handled, and ``refused`` says the manager is to be told."""
    with cabinet_db() as db:
        thread = db.execute(
            Q.thread_of_refusal,
            (thread_id,),
        ).fetchone()
        if not thread:
            return ok("Fil inconnu.", refused=False)
        session_id, sender, subject, claimed_at = thread
        refused = db.execute(
            Q.sender_refused_since,
            (session_id, claimed_at),
        ).fetchone()
        if not refused:
            return ok("Pas de refus.", refused=False)
        db.execute(Q.handle_thread_messages, (thread_id,))
        set_aside(db, thread_id, "ignored", refused[0])
    message = "Travail refuse : l'expediteur n'a pas ce dossier."
    return ok(message, refused=True, sender=sender, subject=subject, reason=refused[0])


def cabinet_manager():
    """The cabinet's manager's address: the "Responsable" of the cabinet's own company in Experio, else
    MAIL_MANAGER, else None. Only an active collaborator counts. Experio has no other place for it."""
    responsable = odoo_call("res.company", "read", [1], fields=["responsable_id"])[0]["responsable_id"]
    if responsable:
        found = odoo_call("res.users", "read", [responsable[0]], fields=["login"])
        if found and collaborator(found[0]["login"]):
            return found[0]["login"]
    fallback = setting("MAIL_MANAGER", "").strip()
    return fallback if fallback and collaborator(fallback) else None


@tool
def notify_manager(sender: str, subject: str, reason: str) -> dict:
    """Tell the cabinet's manager that ``sender`` wrote to Tarik and was refused, at most
    once a day per sender. The sender gets nothing."""
    manager = cabinet_manager()
    if not manager:
        raise ToolError(
            "Manager non prevenu : aucun Responsable sur la societe du cabinet dans Experio, et MAIL_MANAGER "
            "n'est pas l'email d'un collaborateur du cabinet."
        )
    with cabinet_db() as db:
        if db.execute(Q.manager_alerted_today, (sender,)).fetchone():
            return ok(f"Manager deja prevenu aujourd'hui pour {sender}.")
        send_email(
            manager,
            f"Tarik : email de {sender} non traite",
            f"Bonjour,\n\n{sender} m'a ecrit (objet : « {subject or 'sans objet'} »).\n"
            f"Je n'ai pas traite son email : {reason}.\n\nJe ne lui ai envoye aucune reponse.",
        )
        db.execute(Q.insert_mail_alert, (sender,))
    return ok(f"Manager prevenu : {sender}.")


@tool
def finish_thread(thread_id: str, message_ids: list, sent: bool, reason: str = "") -> dict:
    """After Tarik's reply: its emails are answered, and the thread waits for the client
    when Tarik asked a question, or is followed up while drafts Experio was reading or
    dossiers to finish wait (FOLLOW_UP_MINUTES at most). When the reply could not be sent, the thread is set
    aside in error, with ``reason``."""
    with cabinet_db() as db:
        session = db.execute(Q.session_of_thread, (thread_id,)).fetchone()
        if not session:
            return ok("Fil inconnu.")
        if not sent:
            set_aside(db, thread_id, "error", reason or "reponse non envoyee")
            return ok("Fil mis de cote : reponse non envoyee.")
        db.execute(Q.handle_mail_messages, (list(message_ids),))
        newer = db.execute(Q.thread_has_unhandled, (thread_id,)).fetchone()
        waiting = db.execute(Q.session_has_pending, session).fetchone()
        state = "new" if newer else "waiting" if waiting else "done"  # new: an email came in during this run
        # Something left for a follow-up: drafts to take up again, or dossiers to finish.
        if state == "done" and (waiting_setups(session[0])["dossiers"] or drafts_to_resume(session[0])):
            until = db.execute(
                Q.follow_up_still_open,
                (FOLLOW_UP_MINUTES, thread_id),
            ).fetchone()[0]
            state = "follow_up" if until else "done"
        db.execute(
            Q.finish_mail_thread,
            (state, state, FOLLOW_UP_MINUTES, thread_id),
        )
    done = {
        "new": "Fil traite, un nouvel email attend.",
        "waiting": "Fil en attente du client.",
        "follow_up": "Fil traite, la suite suivra.",
        "done": "Fil traite.",
    }
    return ok(done[state])


def untaken(session_id):
    """How many of the conversation's files no tool has taken yet (int): a processed file is taken (kind, invoice or
    statement set) by the tools themselves, so files still untaken after the agent's reply mean it never called the
    files tool, or asked the client first."""
    with cabinet_db() as db:
        return len(db.execute(Q.untaken_file_names, (session_id,)).fetchall())
