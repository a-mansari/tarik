"""Sparrow: Experio's WhatsApp relay, Tarik's WhatsApp channel (SPARROW_URL, SPARROW_SENDER_ID and SPARROW_TOKEN, Vault
secret/tarik/shared).

All of Experio shares one WhatsApp number (one sender). A person is a *recipient* of it: a phone number, the host
Sparrow forwards their messages to (``host_uri``, a bare host: Sparrow calls https://<host_uri>/sparrow/handler with
``Authorization: Bearer <recipient_id>``) and a private ``recipient_id``. Sparrow finds the recipient of an incoming
message by phone alone, so one phone belongs to one host: enroll refuses a phone already registered (a dossier's
WhatsApp number in Experio, or another Tarik user).

Sparrow answers an incoming message's forward within 15 s and never sends it again, so the handler must answer at once.
Sparrow accepts only Moroccan numbers (212 and 9 digits). A text goes only within 24 h of the person's last message.
Calls carry the master token, which can message any recipient: send only to a recipient_id Tarik stored itself."""

import re
from typing import Final

import httpx

from engine.tools.core.cabinets import setting

# Cloudflare in front of Experio's services refuses some clients' default User-Agent.
HEADERS: Final = {"User-Agent": "tarik/1.0"}
TIMEOUT: Final = 15  # seconds, as Sparrow's own calls to WhatsApp
LIMIT: Final = 4096  # characters WhatsApp accepts in one text


class SparrowError(Exception):
    """Sparrow refused, or could not be reached. ``status`` is its HTTP status, None when unreachable. The message
    never holds the token; it stays on the server (log), the client is told in words by the caller."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class PhoneTaken(SparrowError):
    """The phone already is a Sparrow recipient (of a dossier or of another Tarik user)."""


def digits(phone):
    """Return ``phone`` with digits only (Sparrow's form: no +, space or dash)."""
    return re.sub(r"\D+", "", str(phone))


def call(method, path, **kwargs):
    """Return Sparrow's JSON answer to HTTP ``method`` on ``path``, with the master token. Raises SparrowError."""
    headers = {**HEADERS, "Authorization": f"Bearer {setting('SPARROW_TOKEN')}"}
    try:
        answer = httpx.request(
            method, setting("SPARROW_URL").rstrip("/") + path, headers=headers, timeout=TIMEOUT, **kwargs
        )
    except httpx.HTTPError as error:
        raise SparrowError(f"Sparrow unreachable: {type(error).__name__}") from None
    if answer.is_error:
        try:
            detail = answer.json().get("detail", "")
        except ValueError:
            detail = answer.text
        raise SparrowError(f"Sparrow {answer.status_code}: {str(detail)[:200]}", answer.status_code)
    return answer.json()


def recipients_of(phone):
    """Return the recipients registered under ``phone`` ([{"recipient_id", "phone_number", "host_uri", "sender_id",
    "created_at"}]), [] when there is none."""
    try:
        return call("GET", f"/recipients/lookup/{digits(phone)}")
    except SparrowError as error:
        if error.status == 404:
            return []
        raise


def enroll(phone, host_uri, user_name, cabinet_name, template_id=None, template_variables=None):
    """Register ``phone`` as a recipient whose messages go to ``host_uri``, and return its recipient_id. Sparrow sends
    the person its welcome template (``user_name`` and ``cabinet_name`` fill the default one; ``template_id`` with
    ``template_variables`` choose another, a Meta template).

    Raises PhoneTaken when the phone already is a recipient, SparrowError for any other refusal (a number that is not
    212 and 9 digits is a 422)."""
    phone = digits(phone)
    if recipients_of(phone):
        raise PhoneTaken(f"{phone} is already a Sparrow recipient")
    body = {
        "phone_number": phone,
        "host_uri": host_uri,
        "sender_id": setting("SPARROW_SENDER_ID"),
        "user_name": user_name,
        "cabinet_name": cabinet_name,
    }
    if template_id:
        body.update(template_id=template_id, template_variables=template_variables)
    try:
        return call("POST", "/recipients/activate", json=body)["recipient_id"]
    except SparrowError as error:
        if error.status == 409:  # registered between the lookup and now
            raise PhoneTaken(f"{phone} is already a Sparrow recipient") from None
        raise


def remove(recipient_id):
    """Remove the recipient; one already gone is not an error."""
    try:
        call("DELETE", f"/recipients/{recipient_id}")
    except SparrowError as error:
        if error.status != 404:
            raise


def parts(text, limit=LIMIT):
    """Return ``text`` cut into texts of at most ``limit`` characters, at a line break, else a space, else anywhere."""
    text, found = text.strip(), []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        found.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return found + [text] if text else found


def send(recipient_id, text, reply_to=None):
    """Send ``text`` to the recipient, in as many messages as WhatsApp's limit needs (``reply_to``, a WhatsApp message
    id, quotes it on the first). Returns the messages' WhatsApp ids. Raises SparrowError (the 24 h window closed is a
    502)."""
    ids = []
    for part in parts(text):
        body = {"recipient_id": recipient_id, "content": part, "reply_to_message_id": reply_to if not ids else None}
        ids.append(call("POST", "/messages/send", json=body)["message_id"])
    return ids
