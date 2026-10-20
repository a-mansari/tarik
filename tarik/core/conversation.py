"""The client's own words in a conversation: kept for the checks that only accept what the client
wrote themselves (a model may not decide a value or a copy), and the agent's reading of their oui / non."""

from tarik.core.cabinets import cabinet_db
from tarik.core.words import normalized, said

# What ask_agent adds to an email's text for the agent: never the client's own words.
FILES_NOTE, NO_TEXT = "\n\nFichiers joints : ", "(pas de message)"


def own_words(message):
    """The client's own text in a message handed to the agent (str): without the files' names, which no
    client wrote ("ICE 001122334455667.pdf" is not an ICE they gave)."""
    text = (message or "").split(FILES_NOTE)[0]
    return "" if text.strip() == NO_TEXT else text


def remember_client_message(session_id, client_message):
    """Keep the client's message (word for word, never the agent's) for this conversation's later checks."""
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


def chosen_among(decision, choices):
    """The id the agent's ``decision`` names among ``choices`` ({id: name}, as the question listed them and
    record_step kept them): the id itself ("7", "id 7", "n° 7"), or the name as listed. None for anything else,
    nothing proposed included: the question is asked again."""
    choices = {str(key): normalized(name) for key, name in (choices or {}).items()}
    decision = normalized(str(decision or ""))
    for prefix in ("id ", "n "):  # "n° 7" normalizes to "n 7"
        decision = decision.removeprefix(prefix)
    if decision in choices:
        return int(decision)
    named = [key for key, name in choices.items() if name == decision]
    return int(named[0]) if len(named) == 1 else None


def instructed(value, client_message):
    """``value``, an instruction the agent read for an invoice (an account, a payment method, a withholding), when
    the client wrote it themselves in ``client_message`` (its words, whole, or its digits); else "". A model may
    not add an instruction the client did not give."""
    words = normalized(value or "")
    if not words:
        return ""
    message = normalized(own_words(client_message))
    return value if said(words, message) or (words.isdigit() and said(words, message)) else ""


def yes_no(decision):
    """The agent's ``decision`` on a oui / non question, read from the client's answer: True for exactly "oui",
    False for exactly "non", None for anything else (the client was unclear: the question is asked again)."""
    return {"oui": True, "non": False}.get(normalized(decision or ""))
