"""Tarik's answer to a submitted dossier form (form.py): it writes back in the conversation the form came from, so the
client sees their choices were taken, and which of them acted at once.

An email conversation gets a reply in its thread (as a run's reply does: inbox.addressed, sent_in_thread), a WhatsApp
one a text (sparrow.send); a chat conversation none, its page says thanks. The text is a fixed French template, no
model: short, with only what the client chose. Best effort: a failure is logged and never undoes the form."""

import psycopg

from engine.tools.core.app import log
from engine.tools.core.cabinets import cabinet_db, current
from engine.tools.core.config import POSTGRES
from engine.tools.core.queries import Q
from engine.tools.core.sparrow import send
from engine.tools.emails.inbox import addressed, sent_in_thread, thread_of_session
from engine.tools.emails.sending import plain


def text_of(dossier, applied, recorded, posts):
    """Return the confirmation (Markdown) of the form of ``dossier`` (its name): ``applied``, the Experio changes made
    (lines), ``recorded``, the guide answers kept (lines), and ``posts``, whether the form turned posting unasked on
    (True), off (False), or left it (None)."""
    parts = [f"Merci, j'ai bien reçu vos choix pour le dossier « {dossier} »."]
    if applied:
        parts.append("**Appliqué dans Experio :**\n" + "\n".join(f"- {line}" for line in applied))
    if recorded:
        parts.append("**Enregistré pour ce dossier :**\n" + "\n".join(f"- {line}" for line in recorded))
    if posts is True:
        parts.append(
            "Comme vous l'avez choisi, je comptabiliserai désormais les factures vérifiées sans attendre votre demande."
        )
    elif posts is False:
        parts.append("Je ne comptabiliserai plus les factures vérifiées sans votre demande.")
    if recorded:
        parts.append(
            "Ces choix sont gardés pour le dossier : je les suivrai à mesure que je sais le faire. "
            "Dites-moi si vous voulez en changer."
        )
    if not (applied or recorded or posts is not None):
        parts.append("Vos choix sont déjà ceux du dossier : rien n'a changé.")
    return "\n\n".join(parts)


def whatsapp_recipient(session_id):
    """Return the Sparrow recipient_id of WhatsApp conversation ``session_id`` in the current cabinet, or None."""
    with psycopg.connect(**POSTGRES, options="-c search_path=orchestrator", connect_timeout=10) as db:
        row = db.execute(Q.whatsapp_recipient_of_session, (current(), session_id)).fetchone()
    return row[0] if row else None


def send_reply(session_id, text):
    """Write ``text`` in conversation ``session_id``: in its mail thread, or to its WhatsApp recipient. Returns whether
    it was sent (False for a conversation with no such channel)."""
    thread_id = thread_of_session(session_id)
    if thread_id:
        with cabinet_db() as db:
            address, subject = db.execute(Q.thread_reply_basics, (thread_id,)).fetchone()
            reply = addressed(db, thread_id, address, subject)
        sent_in_thread(thread_id, reply, text)
        return True
    recipient = whatsapp_recipient(session_id) if session_id.startswith("wa-") else None
    if recipient:
        send(recipient, plain(text))
        return True
    return False


def confirm(session_id, dossier, applied, recorded, posts):
    """Tell the client in conversation ``session_id`` that their form was taken (text_of). Returns whether a message was
    sent; any failure (a recipient not allowed, SMTP or Sparrow down) is logged and gives False."""
    try:
        return send_reply(session_id, text_of(dossier, applied, recorded, posts))
    except Exception as error:
        log(f"form confirmation not sent ({dossier}): {type(error).__name__}")
        return False
