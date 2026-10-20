"""The client's own words in a conversation: kept for the checks that only accept what the client
wrote themselves (a model may not decide a value or a copy), and the agent's reading of their oui / non."""

from tarik.core.cabinets import cabinet_db
from tarik.core.queries import Q
from tarik.core.words import normalized, said

# What ask_agent adds to an email's text for the agent: never the client's own words.
FILES_NOTE, NO_TEXT = "\n\nFichiers joints : ", "(pas de message)"


def handed(message, files):
    """The client's ``message`` and the names of their ``files`` (list), as one text handed to the agent."""
    return (message or NO_TEXT) + (FILES_NOTE + ", ".join(files) if files else "")


def files_of(message):
    """The names of the files (list) in a message handed to the agent (see handed)."""
    files = (message or "").partition(FILES_NOTE)[2]
    return files.split(", ") if files else []


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
        last = db.execute(Q.last_client_message, (session_id,)).fetchone()
        if not last or last[0] != client_message:
            db.execute(Q.insert_client_message, (session_id, client_message))


def client_messages(session_id, client_message):
    """The client's messages (list of str) in this conversation, the current one included (their own words only)."""
    remember_client_message(session_id, client_message)
    with cabinet_db() as db:
        rows = db.execute(Q.client_messages_of_session, (session_id,))
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
    the client wrote it themselves in ``client_message`` (its words, whole: 612 is not 6125); else "". A model may
    not add an instruction the client did not give."""
    words = normalized(value or "")
    if not words:
        return ""
    message = normalized(own_words(client_message))
    return value if said(words, message) else ""


def yes_no(decision):
    """The agent's ``decision`` on a oui / non question, read from the client's answer: True for exactly "oui",
    False for exactly "non", None for anything else (the client was unclear: the question is asked again)."""
    return {"oui": True, "non": False}.get(normalized(decision or ""))


def kept_answer(invoice_id, step):
    """The client's answer kept for ``step``'s question on ``invoice_id`` ("" when none)."""
    with cabinet_db() as db:
        row = db.execute(Q.kept_answer_of_step, (invoice_id, step)).fetchone()
    return row[0] if row else ""


def keep_answer(invoice_id, step, answer):
    """The client's understood answer to ``step``'s question on ``invoice_id``, kept for the runs taking it up again."""
    with cabinet_db() as db:
        db.execute(
            Q.upsert_answer,
            (invoice_id, step, answer),
        )


def kept_yes_no(invoice_id, step, answer):
    """The client's oui / non to ``step``'s question on ``invoice_id``: (``answer``, else the one kept when it was
    asked before; yes_no of it). A clear one is kept, so the question is not asked again."""
    answer = answer or kept_answer(invoice_id, step)
    told = yes_no(answer)
    if told is not None:
        keep_answer(invoice_id, step, answer)
    return answer, told
