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


def store(db, uid, message):
    """Record one email in its thread (found by In-Reply-To and References), with its files.
    Returns what the cabinet's manager must hear of when its sender may not use Tarik, else None."""
    message_id = (message.get("Message-ID") or f"<uid-{uid}@{folder()}>").strip()
    if db.execute("select 1 from tarik_mail_messages where message_id = %s", (message_id,)).fetchone():
        return
    sender = (parseaddr(message.get("Reply-To", ""))[1] or parseaddr(message.get("From", ""))[1]).lower()
    if sender in own_addresses():
        return  # Tarik's own reply, when the folder holds the sent copies
    linked = re.findall(r"<[^>]+>", f"{message.get('In-Reply-To', '')} {message.get('References', '')}")
    row = db.execute("select thread_id from tarik_mail_messages where message_id = any(%s) limit 1", (linked,)).fetchone()
    subject = str(message.get("Subject", "") or "").strip()
    thread_id = row[0] if row else message_id
    if automatic(message):
        reason, unauthorized = "reponse automatique ou liste de diffusion", None
    elif not recipient_allowed(sender):
        reason = unauthorized = "expediteur hors de la liste autorisee"
    elif not collaborator(sender):
        reason = unauthorized = "expediteur hors des collaborateurs du cabinet dans Experio"
    else:
        reason = unauthorized = None
    if row:  # the client wrote again: back to Tarik, even after an error (the attempts start over)
        db.execute("update tarik_mail_threads set state = 'new', attempts = 0, reason = null, updated_at = now() "
                   "where thread_id = %s and state <> 'ignored'", (thread_id,))
    else:
        db.execute("insert into tarik_mail_threads (thread_id, session_id, client_address, subject, state, reason) "
                   "values (%s, %s, %s, %s, %s, %s)", (thread_id, session_of(thread_id), sender, subject[:300],
                                                       "ignored" if reason else "new", reason))
    session = db.execute("select session_id from tarik_mail_threads where thread_id = %s", (thread_id,)).fetchone()[0]
    db.execute("insert into tarik_mail_messages (message_id, thread_id, uid, from_address, subject, body, references_ids) "
               "values (%s, %s, %s, %s, %s, %s, %s)", (message_id, thread_id, uid, sender, subject[:300], client_text(message),
                                                       " ".join(linked)))
    if not reason:
        for name, mime, data in attachments(message):
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
        messages = db.execute("select message_id, body, references_ids from tarik_mail_messages "
                              "where thread_id = %s and not handled order by received_at, uid", (thread_id,)).fetchall()
        files = [name for (name,) in db.execute(
            "select file_name from tarik_files where session_id = %s and parent_id is null and kind is null "
            "and invoice_id is null and statement_id is null order by id", (session_id,))]
        last_id, last_references = (messages[-1][0], messages[-1][2]) if messages else ("", "")
        threads.append({"thread_id": thread_id, "session_id": session_id, "to": address,
                        "subject": subject if subject.lower().startswith(("re:", "re :")) else f"Re: {subject}",
                        "message": "\n\n".join(body for _, body, _ in messages if body) or "(pas de texte)",
                        "files": files, "message_ids": [message_id for message_id, _, _ in messages],
                        "in_reply_to": last_id, "references": last_references})
    return threads


@server.tool()
def fetch_emails() -> dict:
    """Read the mailbox's new emails (without changing anything there), sort them into
    threads, and hand over the threads waiting for an answer, each locked for this run.
    The first run only notes where the mailbox stands: earlier emails are left alone."""
    box = connect()
    try:
        uidvalidity = int(box.response("UIDVALIDITY")[1][0])
        newest = int(box.response("UIDNEXT")[1][0]) - 1
        with cabinet_db() as db:
            state = db.execute("select uidvalidity, last_uid from tarik_mail_state where folder = %s", (folder(),)).fetchone()
            if not state or state[0] != uidvalidity:  # first run, or the folder was rebuilt: start from now
                db.execute("insert into tarik_mail_state (folder, uidvalidity, last_uid) values (%s, %s, %s) on conflict (folder) "
                           "do update set uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid", (folder(), uidvalidity, newest))
                return ok("Boite aux lettres prise en compte a partir de maintenant.", threads=claim(db), alerts=[])
            uids = [int(uid) for uid in box.uid("search", None, f"UID {state[1] + 1}:*")[1][0].split()]
            alerts = []
            for uid in sorted(u for u in uids if u > state[1]):  # "n:*" also returns the last email when there's nothing new
                data = box.uid("fetch", str(uid), "(BODY.PEEK[])")[1]
                raw = next((part[1] for part in data if isinstance(part, tuple)), None)
                alert = store(db, uid, message_from_bytes(raw, policy=policy.default)) if raw else None
                if alert:
                    alerts.append(alert)
                db.execute("update tarik_mail_state set last_uid = %s where folder = %s", (uid, folder()))
                db.commit()  # each email recorded for good before the next
            threads = claim(db)
    finally:
        box.logout()
    return ok(f"{len(threads)} fil(s) a traiter, {len(alerts)} expediteur(s) refuse(s).", threads=threads, alerts=alerts)


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
        waiting = db.execute("select 1 from tarik_pending where session_id = %s limit 1", session).fetchone()
        db.execute("update tarik_mail_threads set state = %s, claimed_at = null, attempts = 0, reason = null, updated_at = now() "
                   "where thread_id = %s", ("waiting" if waiting else "done", thread_id))
    return ok("Fil en attente du client." if waiting else "Fil traite.")
