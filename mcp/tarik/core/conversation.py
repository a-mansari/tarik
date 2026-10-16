"""The client's own words in a conversation: kept for the checks that only accept what the client
wrote themselves (a model may not decide a value, a skip or a copy), and their yes or no."""

import re

from tarik.core.cabinets import cabinet_db
from tarik.dossiers.search import normalized


# What ask_agent adds to an email's text for the agent: never the client's own words.
FILES_NOTE, NO_TEXT = "\n\nFichiers joints : ", "(pas de message)"


def own_words(message):
    """The client's own text in a message handed to the agent: without the files' names, which no client
    wrote ("ICE 001122334455667.pdf" is not an ICE they gave)."""
    text = (message or "").split(FILES_NOTE)[0]
    return "" if text.strip() == NO_TEXT else text


def said(phrase, words):
    """``phrase`` is in ``words`` as whole words (both normalized): "alpha" is not in "alphanumerique"."""
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", words) is not None


def remember_client_message(session_id, client_message):
    """Keep the client's message (n8n's word for word, never the agent's) for this conversation's later checks."""
    client_message = own_words(client_message)
    if session_id and client_message.strip():
        with cabinet_db() as db:
            last = db.execute("select message from tarik_client_messages where session_id = %s order by id desc limit 1",
                              (session_id,)).fetchone()
            if not last or last[0] != client_message:
                db.execute("insert into tarik_client_messages (session_id, message) values (%s, %s)", (session_id, client_message))


def client_messages(session_id, client_message):
    """The client's messages in this conversation, the current one included (their own words only)."""
    remember_client_message(session_id, client_message)
    with cabinet_db() as db:
        return [text for (text,) in db.execute("select message from tarik_client_messages where session_id = %s order by id",
                                               (session_id,))]



# tarik-master read the yes/no with a model call; these words, in code, are enough and never drift.
YES = ("oui", "yes", "ok", "d'accord", "vas-y", "vas y", "repartis", "repartir", "c'est ca", "exact")
NO = ("non", "no", "pas besoin", "ne pas", "ponctuel", "inutile")


def yes_no(reply):
    """True, False, or None when the reply says neither."""
    text = normalized(reply or "")
    if any(text.startswith(normalized(word)) for word in NO):
        return False
    if any(text.startswith(normalized(word)) for word in YES):
        return True
    return None
