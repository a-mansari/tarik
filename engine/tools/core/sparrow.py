"""Sparrow: Experio's WhatsApp relay, Tarik's WhatsApp channel (SPARROW_URL, SPARROW_SENDER_ID and SPARROW_TOKEN, Vault
secret/tarik/shared).

Experio's dossiers are configured with one WhatsApp sender (SPARROW_SENDER_ID, the sender Tarik enrolls under). A person
is a *recipient* of it: a phone number, the host Sparrow forwards their messages to (``host_uri``, a bare host: Sparrow
calls https://<host_uri>/sparrow/handler with ``Authorization: Bearer <recipient_id>``) and a private ``recipient_id``.
Sparrow finds the recipient of an incoming message by phone alone, across senders, but only refuses a second row for a
phone under the same sender: so enroll looks the phone up first and refuses one already registered (a dossier's WhatsApp
number in Experio, or another Tarik user).

Sparrow answers an incoming message's forward within 15 s and never sends it again, so the handler must answer at once.
Sparrow accepts only Moroccan numbers (212 and 9 digits). WhatsApp allows a text only within 24 h of the person's last
message (a refusal is a 502, its text cut short by Sparrow) and 4096 characters per text.
Calls carry the master token, which can message any recipient: use only a recipient_id Tarik stored itself, and give
``host_uri`` from Tarik's own settings, never from a client or the model. The calls are blocking: from async code,
run them in a thread."""

import contextlib
import re
from typing import Final

import httpx

from engine.tools.core.app import log
from engine.tools.core.cabinets import setting
from engine.tools.core.words import BOLD, CODE, ITALIC, LINK

# IPv4 only, as experio.py's: the container has no IPv6 route, httpx does not fall back, Sparrow has AAAA records.
HTTP: Final = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"), timeout=httpx.Timeout(15, connect=5))
LIMIT: Final = 4096  # characters WhatsApp accepts in one text
# Sparrow's lookup 404 when the phone has no recipient; any other 404 is a wrong route.
NO_RECIPIENT: Final = "No recipient found for provided phone_number"
RECIPIENT_ID: Final = re.compile(r"[A-Za-z0-9]{16}")  # as Sparrow makes them (generate_secure_id)


class SparrowError(Exception):
    """Sparrow refused, or could not be reached. ``status`` is its HTTP status, None when unreachable. The message
    is only the status: Sparrow's own text (``detail``, logged) can echo what was sent, so it stays on the server."""

    def __init__(self, message, status=None, detail=""):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.sent = []  # the WhatsApp ids of the parts of a text already sent when send failed


class PhoneTaken(SparrowError):
    """The phone already is a Sparrow recipient (of a dossier or of another Tarik user)."""


def digits(phone):
    """Return ``phone`` with digits only (Sparrow's form: no +, space or dash)."""
    return re.sub(r"\D+", "", str(phone))


def moroccan(phone):
    """Return ``phone`` as Sparrow takes it (212 and 9 digits) from its international (+212..., 00212..., +212 (0)6...)
    or local (06...) form; None for any other number."""
    number = digits(phone).removeprefix("00")
    if re.fullmatch(r"0\d{9}", number):
        number = "212" + number[1:]
    elif re.fullmatch(r"2120\d{9}", number):  # "+212 (0)6 12 34 56 78": the trunk 0 kept after the country code
        number = "212" + number[4:]
    return number if re.fullmatch(r"212\d{9}", number) else None


REWRITE_LIMIT = 10_000  # characters: a longer reply is sent as it is (the rewrite is quadratic on crafted text)
TRIPLE = re.compile(r"\*\*\*([^*\n]+?)\*\*\*")  # ***text***: bold and italic
STRIKE = re.compile(r"~~([^~\n]+?)~~")
FENCE = re.compile(r"```.*?```", re.S)
HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)  # "## Title", not a dossier's "#857"
BULLET = re.compile(r"^([ \t]*)[*+•][ \t]+", re.M)
RULE = re.compile(r"^[ \t]*([-*_][ \t]*){3,}$", re.M)
BOLD_MARK = "\x01"  # a bold span's mark until the end, so a rewritten * is never read as an italic's


def whatsapp_text(markdown):
    """Return Tarik's reply ``markdown`` as WhatsApp shows it: bold and italic and strike in its own marks (*bold*,
    _italic_, ~strike~), a heading as a bold line, "* " bullets as dashes, a link as "name (address)", a rule dropped,
    blank lines reduced to one. Code spans and fenced code stay as they are (WhatsApp renders them).

    Apply it once, to the reply as Tarik wrote it. A reply over REWRITE_LIMIT is returned as it is. Tables are not
    rewritten: Tarik is not told to write any, and their pipes read well enough."""
    if len(markdown) > REWRITE_LIMIT:
        return markdown
    codes = []

    def keep(found):
        codes.append(found.group(0))
        return f"\x00{len(codes) - 1}\x00"

    text = CODE.sub(keep, FENCE.sub(keep, markdown.replace("\x00", "").replace(BOLD_MARK, "")))
    text = HEADING.sub(lambda found: "**" + found.group(1).replace("**", "") + "**", text)
    text = BULLET.sub(r"\1- ", text)
    text = TRIPLE.sub(rf"{BOLD_MARK}_\1_{BOLD_MARK}", text)
    text = ITALIC.sub(r"_\1_", text)
    text = BOLD.sub(lambda found: f"{BOLD_MARK}{found.group(1) or found.group(2)}{BOLD_MARK}", text)
    text = STRIKE.sub(r"~\1~", text)
    text = LINK.sub(
        lambda found: found.group(2) if found.group(1) == found.group(2) else f"{found.group(1)} ({found.group(2)})",
        text,
    )
    text = RULE.sub("", text).replace(BOLD_MARK, "*")
    text = re.sub(r"\x00(\d+)\x00", lambda found: codes[int(found.group(1))], text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def known(recipient_id):
    """Return ``recipient_id`` when it has the form Sparrow makes, else raise SparrowError: it goes into a URL."""
    if not RECIPIENT_ID.fullmatch(str(recipient_id)):
        raise SparrowError("not a recipient id")
    return recipient_id


def call(method, path, **kwargs):
    """Return Sparrow's JSON answer to HTTP ``method`` on ``path``, with the master token. Raises SparrowError."""
    try:
        url, token = setting("SPARROW_URL").rstrip("/"), setting("SPARROW_TOKEN")
    except KeyError:
        raise SparrowError("Sparrow is not set up") from None
    try:
        answer = HTTP.request(method, url + path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        body = answer.json() if answer.is_success else {}
    except (httpx.HTTPError, ValueError) as error:
        raise SparrowError(f"Sparrow unreachable or unreadable: {type(error).__name__}") from None
    if not answer.is_success:
        try:
            detail = str(answer.json().get("detail", ""))[:200]
        except ValueError, AttributeError:
            detail = answer.text[:200]
        log(f"sparrow: {method} {path.split('/')[1]} answered {answer.status_code}: {detail}")
        raise SparrowError(f"Sparrow answered {answer.status_code}", answer.status_code, detail)
    return body


def lookup(phone):
    """Return the recipients registered under the digits ``phone`` ([{"recipient_id", "host_uri", "sender_id", ...}]),
    [] when Sparrow says there is none. Never returned to callers: other hosts' recipient_ids are secrets."""
    try:
        return call("GET", f"/recipients/lookup/{phone}")
    except SparrowError as error:
        if error.status == 404 and error.detail == NO_RECIPIENT:
            return []
        raise


def enroll(phone, host_uri, user_name, cabinet_name):
    """Register ``phone`` as a recipient whose messages go to ``host_uri`` (Tarik's own host), and return its
    recipient_id. Sparrow sends the person its default welcome template, ``user_name`` and ``cabinet_name`` filling it.

    Raises PhoneTaken when the phone already is a recipient, SparrowError for any other refusal (a number that is not
    Moroccan, see moroccan, is refused before any call). Sparrow saves the recipient before it sends the welcome: when
    the call then fails, the recipient Tarik just made is removed, so a retry is not refused as PhoneTaken with no
    recipient_id to remove."""
    phone = moroccan(phone)
    if phone is None:
        raise SparrowError("not a Moroccan phone number")
    if lookup(phone):
        raise PhoneTaken(f"{phone} is already a Sparrow recipient")
    try:
        sender = setting("SPARROW_SENDER_ID")
    except KeyError:
        raise SparrowError("Sparrow is not set up") from None
    body = {
        "phone_number": phone,
        "host_uri": host_uri,
        "sender_id": sender,
        "user_name": user_name,
        "cabinet_name": cabinet_name,
    }
    try:
        return call("POST", "/recipients/activate", json=body)["recipient_id"]
    except (SparrowError, KeyError) as error:
        if isinstance(error, SparrowError) and error.status == 409:  # registered between the lookup and now
            raise PhoneTaken(f"{phone} is already a Sparrow recipient") from None
        with contextlib.suppress(SparrowError):
            for ours in lookup(phone):
                if ours.get("host_uri") == host_uri and ours.get("sender_id") == sender:
                    remove(ours["recipient_id"])
        if isinstance(error, KeyError):
            raise SparrowError("Sparrow's answer has no recipient_id") from None
        raise


def remove(recipient_id):
    """Remove the recipient; one already gone is not an error."""
    try:
        call("DELETE", f"/recipients/{known(recipient_id)}")
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


def send(recipient_id, text):
    """Send ``text`` (markdown, as Tarik writes: see whatsapp_text) to the recipient, in as many messages as WhatsApp's
    limit needs. Returns the messages' WhatsApp ids. Raises SparrowError whose ``sent`` holds the ids of the parts
    already delivered: a caller that tries again sends the rest only, not those twice."""
    known(recipient_id)
    ids = []
    for part in parts(whatsapp_text(text)):
        try:
            body = {"recipient_id": recipient_id, "content": part}
            ids.append(call("POST", "/messages/send", json=body)["message_id"])
        except SparrowError as error:
            error.sent = ids
            raise
        except KeyError:
            raise SparrowError("Sparrow's answer has no message_id") from None
    return ids
