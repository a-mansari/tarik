"""The client's own words in a conversation: kept for the checks that only accept what the client
wrote themselves (a model may not decide a value, a skip or a copy), and their yes or no."""

from tarik.core.cabinets import cabinet_db
from tarik.core.text import normalized

# What ask_agent adds to an email's text for the agent: never the client's own words.
FILES_NOTE, NO_TEXT = "\n\nFichiers joints : ", "(pas de message)"
# tarik-master read the yes/no with a model call; these words, in code, are enough and never drift.
YES = ("oui", "yes", "ok", "d'accord", "vas-y", "vas y", "repartis", "repartir", "c'est ca", "exact")
NO = ("non", "no", "pas besoin", "ne pas", "ponctuel", "inutile")


def own_words(message):
    """The client's own text in a message handed to the agent (str): without the files' names, which no
    client wrote ("ICE 001122334455667.pdf" is not an ICE they gave)."""
    text = (message or "").split(FILES_NOTE)[0]
    return "" if text.strip() == NO_TEXT else text


def remember_client_message(session_id, client_message):
    """Keep the client's message (n8n's word for word, never the agent's) for this conversation's later checks."""
    client_message = own_words(client_message)
    if not (session_id and client_message.strip()):
        return
    with cabinet_db() as db:
        last = db.execute(
            "select message from tarik_client_messages where session_id = %s order by id desc limit 1", (session_id,)
        ).fetchone()
        if not last or last[0] != client_message:
            db.execute(
                "insert into tarik_client_messages (session_id, message) values (%s, %s)", (session_id, client_message)
            )


def client_messages(session_id, client_message):
    """The client's messages (list of str) in this conversation, the current one included (their own words only)."""
    remember_client_message(session_id, client_message)
    with cabinet_db() as db:
        rows = db.execute("select message from tarik_client_messages where session_id = %s order by id", (session_id,))
        return [text for (text,) in rows]


def yes_no(reply):
    """True for a yes, False for a no, None when ``reply`` says neither."""
    text = normalized(reply or "")
    if text.startswith(tuple(map(normalized, NO))):
        return False
    if text.startswith(tuple(map(normalized, YES))):
        return True
    return None
