"""Read Tarik's mailbox over IMAP without changing it, and track in Postgres what Tarik did with each email.

The folder is opened read-only and bodies are fetched with BODY.PEEK, so no flag is set and nothing is moved. The
only thing Tarik adds to the mailbox is a copy of each email it sends, in the Sent folder
(engine.tools.emails.sending.save_sent). Progress is tracked thread by thread in Postgres, so an email someone
already opened in the mailbox is still answered, and a thread waiting for the client comes back when the client
replies.

Tarik answers only the cabinet's collaborators and Experio's own staff, and only when the whitelist
(MAIL_ALLOWED_RECIPIENTS) lets Tarik write to them. Any other email is set aside without being read by the agent, so
it creates nothing in Experio."""

import contextlib
import hashlib
import re
from email import message_from_bytes, policy
from email.utils import parseaddr

import psycopg

from engine.tools.core.app import ToolError, log, ok, tool
from engine.tools.core.archives import unpacked
from engine.tools.core.cabinets import cabinet_db, current, setting
from engine.tools.core.config import FILE_TYPES, READING
from engine.tools.core.experio import odoo_call
from engine.tools.core.queries import Q
from engine.tools.core.words import strip_tags
from engine.tools.dossiers.people import collaborator
from engine.tools.dossiers.setup import waiting_setups
from engine.tools.emails.sending import imap_session, recipient_allowed, send_email
from engine.tools.invoices.pipeline import waiting_drafts

LEASE_MINUTES = 30  # a run longer than this is taken as dead: the thread is picked up again
MAX_ATTEMPTS = 3
FOLLOW_UP_MINUTES = 30  # how long after Tarik's reply the drafts Experio was reading are still followed up
# The acknowledgement of a new thread, sent before the agent works on it (vouvoiement, no civility, as Tarik writes).
ACKNOWLEDGEMENT = (
    "Votre message est bien reçu. Je le traite et vous réponds dans ce fil, en général d'ici quelques minutes."
)

# Where the client's own text stops and the quoted history begins: a line starting with "Le ... a écrit :",
# "On ... wrote:", "-- Message d'origine" / "-- Original message", a quoted header ("De :", "From:", "Envoyé :"), or
# Outlook's line of underscores over it.
QUOTE_START = re.compile(
    r"^\s*(le .{0,120} a [ée]crit\s*:|on .{0,120} wrote\s*:|-{2,}\s*(message d'origine|original message)"
    r"|de\s*:\s|from\s*:\s|envoy[ée]\s*:\s|_{10,}\s*$)",
    re.IGNORECASE,
)
# Where an HTML line ends: <br>, </p>, </div>, a list item's, a table row's, a heading's, a rule.
LINE_END_TAG = re.compile(r"<(br|hr|/p|/div|/li|/tr|/h[1-6])[^>]*>", re.IGNORECASE)
HIDDEN = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)  # a style or script element
BLANK_LINES = re.compile(r"\n{3,}")  # more than one blank line in a row
# Where an email's text shows an image it holds inline (a signature's logo, icons): « [Image] », « [logo.png] »,
# « [cid:image001.png@…] ». Never the client's files; left in, they read as an attachment that is not one.
IMAGE_MARK = re.compile(r"\[(?:image|cid:[^\]\n]*|[^\]\n]*\.(?:png|jpe?g|gif|bmp))\]", re.IGNORECASE)


def without_images(text):
    """Return ``text`` without its inline image marks (IMAGE_MARK), each line's leftover spaces trimmed."""
    return "\n".join(IMAGE_MARK.sub("", line).strip() for line in text.splitlines())


AUTOMATIC_SENDER = re.compile(r"(no-?reply|mailer-daemon|postmaster)@")  # noreply@, no-reply@, mailer-daemon@...
MESSAGE_ID = re.compile(r"<[^>]+>")  # one <message id> in In-Reply-To or References


def folder():
    """Return the current cabinet's mailbox folder (IMAP_FOLDER, default INBOX)."""
    return setting("IMAP_FOLDER", "INBOX")


@contextlib.contextmanager
def connect():
    """Context manager: yield the IMAP session with the cabinet's folder opened read-only (see imap_session)."""
    with imap_session() as box:
        box.select(f'"{folder()}"', readonly=True)
        yield box


def body_text(message):
    """Return the email's body as text: its plain-text part, else its HTML part without tags; "" when it has neither."""
    part = message.get_body(preferencelist=("plain", "html"))
    if not part:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = strip_tags(HIDDEN.sub("", LINE_END_TAG.sub("\n", text)))
    return text


def unquoted_lines(text):
    """Return the lines of ``text`` that are not quoted history.

    Lines starting with « > » are dropped. A quote header (QUOTE_START) followed by « > » lines is skipped, since the
    client may answer below or between the quoted lines; a quote header followed by unmarked text (Outlook) ends the
    client's part: nothing after it is kept."""
    lines, all_lines = [], text.splitlines()
    for at, line in enumerate(all_lines):
        if QUOTE_START.match(line):
            following = next((later for later in all_lines[at + 1 :] if later.strip()), "")
            if not following.lstrip().startswith(">"):
                break  # the earlier email follows unmarked (Outlook): nothing after it is the client's
            continue  # « Le … a écrit : » over a « > » quote: the client may answer below it, or between its lines
        if not line.lstrip().startswith(">"):  # a quoted line
            lines.append(line.rstrip())
    return lines


def client_text(message):
    """Return the client's own words in ``message``, at most 4000 characters.

    Uses the plain text (or the HTML without tags), with the quoted history and inline image marks removed. Text the
    client writes below a « > » quote, or between its lines, is kept."""
    lines = unquoted_lines(body_text(message))
    return BLANK_LINES.sub("\n\n", without_images("\n".join(lines))).strip()[:4000]


SIGNATURE_LINES = 3  # a shorter block two emails end with is more likely the client's own words ("achat", a name)


def same_ending(lines, theirs):
    """Return how many trailing lines ``lines`` and ``theirs`` (lists of str) share, ignoring surrounding spaces."""
    same = 0
    while same < min(len(lines), len(theirs)) and lines[-1 - same].strip() == theirs[-1 - same].strip():
        same += 1
    return same


def without_signature(text, earlier):
    """Return ``text`` without the signature it shares with one of the sender's ``earlier`` emails (list of str).

    The signature is the longest run of trailing lines that ``text`` and an earlier email have in common (name,
    title, phone, links). It is cut only when it holds at least SIGNATURE_LINES non-empty lines and leaves some text;
    otherwise ``text`` is returned unchanged. An earlier email identical to ``text`` is ignored."""
    lines = text.splitlines()
    shared = 0
    for other in earlier:
        same = same_ending(lines, other.splitlines())
        if same < len(lines):  # the same email again (« achat » and the signature, twice) says nothing of where it ends
            shared = max(shared, same)
    cut = len(lines) - shared
    kept, signature = lines[:cut], lines[cut:]
    if sum(1 for line in signature if line.strip()) < SIGNATURE_LINES or not "".join(kept).strip():
        return text
    return "\n".join(kept).strip()


def attachments(message):
    """Return the files the client sent in ``message``, as (files, skipped).

    Returns:
        files: [(name, mime type, bytes)] for supported types (FILE_TYPES: invoices, statements, FEC...); each zip is
            replaced by the files inside it (engine.tools.core.archives.unpacked).
        skipped: messages (str) for a named file of an unsupported type, and for what a zip left out and why.
    Inline images (signature logos) and unnamed parts are dropped silently: they are never the client's files."""
    found, other = [], []
    for part in message.iter_attachments():
        name = part.get_filename() or ""
        inline_image = part.get_content_maintype() == "image" and part.get_content_disposition() == "inline"
        if inline_image or not name:
            continue
        if not name.lower().endswith((*FILE_TYPES, ".zip")):
            other.append(f"{name} : type non pris en charge")
            continue
        found.append((name, part.get_content_type(), part.get_payload(decode=True) or b""))
    files, skipped = unpacked(found, FILE_TYPES)
    return files, other + skipped


def automatic(message):
    """True when ``message`` is an out-of-office reply, a bounce or a mailing-list email: never answered, to avoid reply
    loops."""
    sender = parseaddr(message.get("From", ""))[1].lower()
    return (
        message.get("Auto-Submitted", "no").lower() != "no"
        or message.get("Precedence", "").lower() in ("bulk", "list", "junk")
        or bool(message.get("X-Autoreply") or message.get("List-Id"))
        or AUTOMATIC_SENDER.match(sender) is not None
    )


def without_nul(text):
    """Return ``text`` without NUL characters: Postgres text cannot hold one."""
    return text.replace("\x00", "")


class UnreadableEmail(Exception):
    """Raised when Python cannot read an email (unknown charset, broken header): it is skipped and never tried again."""


def read_email(message_id, message):
    """Return what ``message`` holds, as (sender, reply_to, subject, linked, text, files, skipped, auto).

    Returns:
        sender, reply_to: lowercased bare addresses; reply_to is the sender when there is no Reply-To.
        linked: the message ids named in In-Reply-To and References.
        text: the client's own words (client_text); files and skipped: as attachments returns them.
        auto: True for an automatic email (automatic).
    Raises:
        UnreadableEmail: anything in the email Python cannot read (``message_id`` names it in the error)."""
    try:
        sender = parseaddr(message.get("From", ""))[1].lower()
        reply_to = parseaddr(message.get("Reply-To", ""))[1].lower() or sender
        subject = str(message.get("Subject", "") or "").strip()
        linked = MESSAGE_ID.findall(f"{message.get('In-Reply-To', '')} {message.get('References', '')}")
        text = without_nul(client_text(message))
        files, skipped = attachments(message)
        auto = automatic(message)
        sender, reply_to, subject = without_nul(sender), without_nul(reply_to), without_nul(subject)
        files = [(without_nul(name), mime, data) for name, mime, data in files]
        skipped = [without_nul(why) for why in skipped]
    except Exception as error:
        raise UnreadableEmail(f"{message_id}: {type(error).__name__}: {error}") from None
    return sender, reply_to, subject, linked, text, files, skipped, auto


def refusal(sender, auto):
    """Return why an email from ``sender`` is not for Tarik, as (reason, unauthorized).

    reason is set whenever Tarik must not answer; unauthorized is set too when the cabinet's manager must be told (a
    sender off the whitelist, or neither a cabinet collaborator nor Experio staff). Both are None when the sender may
    use Tarik; an automatic email (``auto``) gives a reason only."""
    if auto:
        return "reponse automatique ou liste de diffusion", None
    if not recipient_allowed(sender):
        return "expediteur hors de la liste autorisee", "expediteur hors de la liste autorisee"
    if not collaborator(sender):
        reason = "expediteur ni collaborateur du cabinet ni de l'equipe Experio"
        return reason, reason
    return None, None


def thread_of(db, linked, sender, subject, reason, message_id):
    """Return the id of the sender's thread this email belongs to, creating the thread when needed.

    The thread is the sender's one that holds one of the ``linked`` message ids (one of their emails, or one Tarik
    sent). When none does, a new thread is created with ``message_id`` as its id, in state "ignored" when there is a
    refusal ``reason``, else "new". An existing thread the client writes in again without a ``reason`` goes back to
    "new" for Tarik."""
    row = db.execute(
        Q.thread_of_linked_message,
        {"linked": linked, "sender": sender},
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


def own_addresses():
    """Return Tarik's own addresses (SMTP_FROM, SMTP_USER, IMAP_USER), lowercased, as a set."""
    own = (parseaddr(setting("SMTP_FROM", ""))[1], setting("SMTP_USER", ""), setting("IMAP_USER", ""))
    return {address.lower() for address in own if address}


def store(db, uid, message):
    """Store one email (``message``, IMAP ``uid``) in its sender's thread, with its files, and return an alert or None.

    The thread is found by In-Reply-To and References (thread_of). The sender is the From address alone: Reply-To
    only says where the reply goes. A reply from someone else in a thread (a colleague in copy) starts that person's
    own thread, with their own rights. A refused email is stored marked as ignored, and its files are not stored.
    Emails already stored, and Tarik's own sent copies, are skipped.

    Returns {"sender", "subject", "reason"} when the cabinet's manager must be told the sender may not use Tarik,
    else None."""
    message_id = (message.get("Message-ID") or f"<uid-{uid}@{folder()}>").strip()
    if db.execute(Q.mail_message_exists, (message_id,)).fetchone():
        return
    sender, reply_to, subject, linked, text, files, skipped, auto = read_email(message_id, message)
    if sender in own_addresses():
        return  # Tarik's own reply, when the folder holds the sent copies
    reason, unauthorized = refusal(sender, auto)
    thread_id = thread_of(db, linked, sender, subject, reason, message_id)
    session = db.execute(Q.session_of_thread, (thread_id,)).fetchone()[0]
    db.execute(
        Q.insert_mail_message,
        (
            message_id,
            thread_id,
            uid,
            sender,
            reply_to,
            subject[:300],
            text,
            " ".join(linked),
            reason is not None,
            skipped,
        ),
    )
    if not reason:
        for name, mime, data in files:
            db.execute(
                Q.insert_mail_file,
                (session, name, mime, data),
            )
    return {"sender": sender, "subject": subject, "reason": unauthorized} if unauthorized else None


def unanswered_text(messages, theirs):
    """Return the client's unanswered ``messages`` joined into one text, each without its signature.

    ``messages`` is rows of (message_id, body, skipped); ``theirs`` is rows of (message_id, body) for all the
    sender's emails, used to find the signature (without_signature). Returns "(pas de texte)" when no message has
    text."""
    bodies = []
    for message_id, body, _ in messages:
        if body:
            others = [other for at, other in theirs if at != message_id]
            bodies.append(without_signature(body, others))
    return "\n\n".join(bodies) or "(pas de texte)"


def thread_item(db, thread_id, session_id, address, subject):
    """Return a claimed thread as the workflow works on it (dict).

    Keys: thread_id, session_id; to (the Reply-To when whitelisted, else ``address``); subject (with « Re: »);
    message (the client's unanswered emails as one text); files (names of the conversation's files not taken yet);
    skipped (what the zips left out); message_ids (the unanswered emails); follow_up (True when there is no
    unanswered email: Tarik takes up the drafts Experio was reading); in_reply_to and references (from the thread's
    last email, for the reply's headers)."""
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
    # Their other emails, signature included: the body is kept whole, the signature cut where Tarik reads it.
    theirs = db.execute(Q.emails_of_sender, (address,)).fetchall()
    skipped = []
    for _, _, left in messages:  # what each email's zips left out
        skipped.extend(left)
    return {
        "thread_id": thread_id,
        "session_id": session_id,
        "to": reply_to if reply_to and recipient_allowed(reply_to) else address,
        "subject": subject if subject.lower().startswith(("re:", "re :")) else f"Re: {subject}",
        "message": unanswered_text(messages, theirs),
        "files": [name for (name,) in files],
        "skipped": skipped,
        "message_ids": [message_id for message_id, _, _ in messages],
        "follow_up": not messages,
        "in_reply_to": last_id,
        "references": last_references,
    }


def claim(db):
    """Lock for this run the threads Tarik must answer and return them (list of thread_item dicts).

    First sets aside in error the threads whose runs failed MAX_ATTEMPTS times. Claims new threads, threads whose run
    died (lease of LEASE_MINUTES over) and follow-up threads that are ready (follow_ups_ready)."""
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
    """Return the invoice ids of the conversation's drafts to take up again; [] when Experio cannot be reached (logged).
    Return the invoice ids of the conversation's drafts to take up again; [] when Experio cannot be reached (logged)."""
    try:
        return waiting_drafts(session_id)["invoices"]
    except Exception as error:
        log(f"drafts_to_resume {session_id}: {type(error).__name__}: {error}")
        return []


def follow_ups_ready(db):
    """Return the ids of the follow-up threads to take now.

    A thread is ready when it has a dossier to finish, when Experio has finished reading every draft its conversation
    waits on (bluejay_state no longer in READING), or when its follow-up time is up. A thread with no draft left
    (posted or deleted in Experio meanwhile) is marked done. When Experio cannot be reached, the thread is checked
    again at the next tick."""
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
    """Return the uid of the last email read in the folder, or None on a first reading.

    A first reading, or a rebuilt folder (``uidvalidity`` changed), records ``newest`` (the folder's newest uid) as
    read and returns None, so earlier emails are left alone."""
    state = db.execute(Q.mail_folder_state, (folder(),)).fetchone()
    if state and state[0] == uidvalidity:
        return state[1]
    db.execute(
        Q.upsert_mail_folder_state,
        (folder(), uidvalidity, newest),
    )
    return None


def uids_after(box, last):
    """Return the folder's uids greater than ``last``, sorted."""
    found = box.uid("search", None, f"UID {last + 1}:*")[1][0]  # (status, [b"uid uid ..."])
    uids = [int(uid) for uid in found.split()]
    return sorted(uid for uid in uids if uid > last)  # "n:*" also returns the last email when there's nothing new


def fetched(box, uid):
    """Return email ``uid``'s raw bytes, fetched without marking it read; None when the server returned none."""
    data = box.uid("fetch", str(uid), "(BODY.PEEK[])")[1]  # (status, data): the email is the tuple in data
    return next((part[1] for part in data if isinstance(part, tuple)), None)


def store_new(box, db, last):
    """Store each email after uid ``last``, committing each before the next, and return the manager alerts (list of
    store results).

    An unreadable email is skipped for good. Any other failure (Experio or the database out) stops the loop: that
    email and the following ones are read at the next reading."""
    alerts = []
    for uid in uids_after(box, last):
        try:
            raw = fetched(box, uid)
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


def folder_number(box, code):
    """Return the number the server sent for ``code`` (UIDVALIDITY, UIDNEXT) when the folder was opened."""
    return int(box.response(code)[1][0])  # (code, [b"number"])


def read_mailbox():
    """Store the mailbox's new emails in their threads without changing the mailbox, and return the manager alerts.

    Returns the list of alerts for refused senders (see store); [] on a first reading, which only records where the
    mailbox stands, and [] when another reading of this cabinet holds the lock. Run by the mail watcher
    (engine/mail.py), never by a workflow, so no run waits on it."""
    with connect() as box, cabinet_db() as db:
        uidvalidity = folder_number(box, "UIDVALIDITY")
        newest = folder_number(box, "UIDNEXT") - 1
        # This cabinet's reading lock, held until db closes: the watcher's wake-up and its tick may overlap.
        if not db.execute(Q.try_mailbox_lock, (f"read_mailbox {current()}",)).fetchone()[0]:
            return []
        last = last_read(db, uidvalidity, newest)
        return [] if last is None else store_new(box, db, last)


def threads_waiting():
    """True when a thread waits for Tarik's answer or a follow-up is ready, so "Tarik - Emails" has work."""
    with cabinet_db() as db:
        waiting = db.execute(Q.any_thread_waiting, (LEASE_MINUTES,)).fetchone()
        return waiting is not None or bool(follow_ups_ready(db))


def set_aside(db, thread_id, state, reason):
    """Unlock thread ``thread_id`` in ``state`` ("ignored" or "error"), with ``reason`` (cut to 300 characters)."""
    db.execute(
        Q.set_aside_thread,
        (state, reason[:300], thread_id),
    )


@tool
def acknowledge_thread(thread_id: str) -> dict:
    """Send the client an acknowledgement of their new thread, in that thread, before Tarik works on it.

    Sent once per thread, and only for a thread Tarik never answered: a reply in an existing thread or a follow-up
    gets none. Returns an ok result with sent=True when sent, sent=False otherwise."""
    with cabinet_db() as db:
        row = db.execute(
            Q.thread_to_acknowledge,
            (thread_id,),
        ).fetchone()
        if not row:
            return ok("Pas d'accuse de reception : fil deja suivi.", sent=False)
        session_id, address, subject, to_acknowledge = row
        if not to_acknowledge:
            return ok("Pas d'accuse de reception : fil deja suivi.", sent=False)
        item = thread_item(db, thread_id, session_id, address, subject)
    result = send_email(item["to"], item["subject"], ACKNOWLEDGEMENT, item["in_reply_to"], item["references"])
    with cabinet_db() as db:
        db.execute(Q.set_thread_acknowledged, (thread_id,))
        if (result or {}).get("message_id"):
            db.execute(Q.insert_mail_sent, (result["message_id"], thread_id))
    return ok("Accuse de reception envoye.", sent=True)


@tool
def claim_threads() -> dict:
    """Lock the threads waiting for Tarik's answer for this run and return them (ok result, threads: thread_item dicts).
    Lock the threads waiting for Tarik's answer for this run and return them (ok result, threads: thread_item dicts)."""
    with cabinet_db() as db:
        threads = claim(db)
    return ok(f"{len(threads)} fil(s) a traiter.", threads=threads)


@tool
def thread_refusal(thread_id: str) -> dict:
    """Check, after Tarik's run on a thread, whether the work was refused because the sender lacks the dossier
    (check_sender).

    When it was, nothing goes back to the sender: the thread's emails are marked handled, the thread is set aside as
    ignored, and the ok result has refused=True with sender, subject and reason for the manager alert. Otherwise
    refused=False."""
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
    """Return the cabinet manager's email address, or None.

    Uses the « Responsable » (responsable_id) of the cabinet's own company in Experio (res.company id 1), else
    MAIL_MANAGER; either counts only when it is an active collaborator or Experio staff member (collaborator).
    Experio has no other field for the cabinet's manager."""
    responsable = odoo_call("res.company", "read", [1], fields=["responsable_id"])[0]["responsable_id"]
    if responsable:
        found = odoo_call("res.users", "read", [responsable[0]], fields=["login"])
        if found and collaborator(found[0]["login"]):
            return found[0]["login"]
    fallback = setting("MAIL_MANAGER", "").strip()
    return fallback if fallback and collaborator(fallback) else None


@tool
def notify_manager(sender: str, subject: str, reason: str) -> dict:
    """Tell the cabinet's manager that ``sender`` wrote to Tarik and was refused (``subject`` and ``reason`` included),
    at most once a day per sender. The sender gets nothing.

    Raises:
        ToolError: no manager address (cabinet_manager returned None)."""
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


def thread_state(db, thread_id, session):
    """Return where the thread stands after Tarik's reply.

    Returns:
        "new": an email came in during this run.
        "waiting": the conversation has a pending question for the client.
        "follow_up": drafts to take up again or dossiers to finish, within FOLLOW_UP_MINUTES of the reply.
        "done": nothing left.
    ``session`` is the (session_id,) row of the thread."""
    newer = db.execute(Q.thread_has_unhandled, (thread_id,)).fetchone()
    waiting = db.execute(Q.session_has_pending, session).fetchone()
    if newer:
        return "new"
    if waiting:
        return "waiting"
    if not (waiting_setups(session[0])["dossiers"] or drafts_to_resume(session[0])):
        return "done"
    until = db.execute(
        Q.follow_up_still_open,
        (FOLLOW_UP_MINUTES, thread_id),
    ).fetchone()[0]
    return "follow_up" if until else "done"


@tool
def finish_thread(thread_id: str, message_ids: list, sent: bool, reason: str = "", sent_id: str = "") -> dict:
    """Close Tarik's run on a thread after the reply.

    When the reply was sent, ``message_ids`` are marked answered and the thread becomes "new", "waiting", "follow_up"
    or "done" (thread_state). When it was not (``sent`` False), the thread is set aside in error with ``reason``.
    ``sent_id`` is the Message-ID of Tarik's reply, recorded so the client's next email can name it alone."""
    with cabinet_db() as db:
        session = db.execute(Q.session_of_thread, (thread_id,)).fetchone()
        if not session:
            return ok("Fil inconnu.")
        if sent_id:
            db.execute(Q.insert_mail_sent, (sent_id, thread_id))
        if not sent:
            set_aside(db, thread_id, "error", reason or "reponse non envoyee")
            return ok("Fil mis de cote : reponse non envoyee.")
        db.execute(Q.handle_mail_messages, (list(message_ids),))
        state = thread_state(db, thread_id, session)
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
    """Return how many of the conversation's files no tool has taken yet (int).

    Tools mark a file taken when they process it (kind, invoice or statement set), so files still untaken after the
    agent's reply mean it never called the files tool, or asked the client first."""
    with cabinet_db() as db:
        return len(db.execute(Q.untaken_file_names, (session_id,)).fetchall())
