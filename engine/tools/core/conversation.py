"""The client's own words in a conversation, kept for the checks that only accept what the client wrote themselves
(a model may not decide a value or a copy), and the agent's reading of the client's oui / non answers."""

import re

from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized, said

# What ask_agent adds to an email's text for the agent: never the client's own words.
FILES_NOTE, NO_TEXT = "\n\nFichiers joints : ", "(pas de message)"
# What Tarik does not take of what the client sent (a file's type, what a zip held), and why.
SKIPPED_NOTE = "\n\nFichiers laissés de côté : "


def handed(message, files, skipped=()):
    """Return one text for the agent: the client's ``message``, then the names in ``files`` (list of str), then the
    ``skipped`` notes ("name : why", from engine.tools.core.archives)."""
    text = message or NO_TEXT
    if files:
        text += FILES_NOTE + ", ".join(files)
    if skipped:
        text += SKIPPED_NOTE + " ; ".join(skipped)
    return text


def left_out(skipped):
    """Return a paragraph listing the files Tarik left out of what the client sent (``skipped``: archives' notes), to
    end a reply with; "" when nothing was left out. The code adds it, so the model cannot forget it."""
    if not skipped:
        return ""
    return "\n\n**Fichiers laissés de côté**\n" + "\n".join(f"- {named(why)}" for why in skipped)


def named(why):
    """Return note ``why`` (« name : why ») with its file name in backticks, like every file name the client reads."""
    name, sep, reason = why.partition(" : ")
    return f"`{name}` : {reason}" if sep else why


def for_client(reply):
    """Return Tarik's ``reply`` without the « QUESTION : » marks the agent recognizes questions by
    (engine.tools.invoices.pipeline.QUESTION_MARK), which mean nothing to the client."""
    return (reply or "").replace("QUESTION : ", "")


def files_of(message):
    """Return the file names (list of str) listed in a message built by handed."""
    files = (message or "").partition(FILES_NOTE)[2].split(SKIPPED_NOTE)[0]
    return files.split(", ") if files else []


# How a client says they attached something (normalized): only then is a missing attachment told to them.
ATTACHED = re.compile(r"(?<![a-z])(?:ci jointe?s?|pieces? jointes?|pj|jointe?s?|voici|en annexe|attachee?s?)(?![a-z])")


def mentions_attachment(message):
    """True when the client's ``message`` says it carries files ("ci-joint", "voici la facture"...) and nothing they
    sent was left out (that is already reported to the client, file by file)."""
    return SKIPPED_NOTE not in (message or "") and ATTACHED.search(normalized(own_words(message))) is not None


def without_subject(text):
    """Return the client's ``text`` without the « Objet : … » first paragraph engine.emails puts an email's subject
    in, to quote the client's answer back to them."""
    text = (text or "").strip()
    head, sep, rest = text.partition("\n\n")
    return rest.strip() if sep and head.startswith("Objet : ") else text


def own_words(message):
    """Return the client's own text (str) in a message built by handed: without the file names and skipped notes,
    which the client did not write ("ICE 001122334455667.pdf" is not an ICE they gave); "" for NO_TEXT."""
    text = (message or "").split(FILES_NOTE)[0].split(SKIPPED_NOTE)[0]
    return "" if text.strip() == NO_TEXT else text


def remember_client_message(session_id, client_message):
    """Store the client's own words in ``client_message`` for conversation ``session_id``, for later checks; nothing
    without a session, without text, or when it repeats the last one stored."""
    client_message = own_words(client_message)
    if not (session_id and client_message.strip()):
        return
    with cabinet_db() as db:
        last = db.execute(Q.last_client_message, (session_id,)).fetchone()
        if not last or last[0] != client_message:
            db.execute(Q.insert_client_message, (session_id, client_message))


def client_messages(session_id, client_message):
    """Return the client's messages (list of str, own words only) in conversation ``session_id``, after storing the
    current ``client_message``."""
    remember_client_message(session_id, client_message)
    with cabinet_db() as db:
        rows = db.execute(Q.client_messages_of_session, (session_id,))
        return [text for (text,) in rows]


def chosen_among(decision, choices):
    """Return the id (int) the agent's ``decision`` names among ``choices``.

    Args:
        decision: the agent's reading of the client's answer: an id ("7", "id 7", "n° 7") or a name as listed.
        choices: {id: name} as the question listed them and record_step kept them.

    Returns None when the decision names no choice or several (the question is then asked again)."""
    choices = {str(key): normalized(name) for key, name in (choices or {}).items()}
    decision = normalized(str(decision or ""))
    for prefix in ("id ", "n "):  # "n° 7" normalizes to "n 7"
        decision = decision.removeprefix(prefix)
    if decision in choices:
        return int(decision)
    matches = [key for key, name in choices.items() if name == decision]
    if len(matches) != 1:
        return None
    return int(matches[0])


def instructed(value, client_message):
    """Return ``value``, an instruction the agent read for an invoice (an account, a payment method, a withholding),
    when the client wrote it in ``client_message`` as whole words (612 is not 6125); else "". A model may not add
    an instruction the client did not give."""
    words = normalized(value or "")
    if not words:
        return ""
    message = normalized(own_words(client_message))
    return value if said(words, message) else ""


def yes_no(decision):
    """Return the agent's ``decision`` on a oui / non question as True ("oui"), False ("non") or None (anything else:
    the client was unclear and the question is asked again)."""
    return {"oui": True, "non": False}.get(normalized(decision or ""))


def kept_answer(invoice_id, step):
    """Return the client's stored answer to ``step``'s question on ``invoice_id``; "" when none."""
    with cabinet_db() as db:
        row = db.execute(Q.kept_answer_of_step, (invoice_id, step)).fetchone()
    return row[0] if row else ""


def keep_answer(invoice_id, step, answer):
    """Store the client's understood ``answer`` to ``step``'s question on ``invoice_id``, for later runs."""
    with cabinet_db() as db:
        db.execute(
            Q.upsert_answer,
            (invoice_id, step, answer),
        )


def kept_yes_no(invoice_id, step, answer):
    """Read the client's oui / non to ``step``'s question on ``invoice_id``; a clear one is stored so the question is
    not asked again.

    Returns (answer, told): answer is ``answer`` when given, else the one stored earlier ("" when none); told is
    yes_no(answer): True, False, or None when unclear."""
    answer = answer or kept_answer(invoice_id, step)
    told = yes_no(answer)
    if told is not None:
        keep_answer(invoice_id, step, answer)
    return answer, told
