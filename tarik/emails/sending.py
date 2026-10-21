"""Tarik's emails: replies go out through SMTP, only to whitelisted addresses.

MAIL_ALLOWED_RECIPIENTS lists who may receive an email from Tarik: full addresses, "@domain"
for a whole domain, or patterns with "*" ("test.*@experio.ma"; "*" alone lets anyone through).
Empty means nobody: no email leaves until someone is listed.

MAIL_CERT_SHA256 pins the mail server's certificate when it has no certificate a known
authority signed (mailcow's own, self-signed): only that exact certificate is accepted, so
the connection stays safe from interception. Empty: the usual verification.
"""

import contextlib
import hashlib
import html
import imaplib
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from tarik.core.app import ToolError, log, ok, tool
from tarik.core.cabinets import current, setting
from tarik.core.experio import odoo_call
from tarik.core.words import strip_tags

MAIL_TIMEOUT = 60  # seconds before the mail server is given up

BOLD = re.compile(r"\*\*(.+?)\*\*|(?<!\w)__(.+?)__(?!\w)")  # **text** or __text__, never inside a word (a__b__c)
# *text*: single stars, not inside a word nor next to another star, the text not starting or ending with a space.
ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
CODE = re.compile(r"`([^`\n]+)`")  # `text` on one line: a file name, kept as written
CODE_HTML = '<code style="background:#f2f2f2;padding:1px 4px;border-radius:3px">{}</code>'
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")  # [label](http(s) address)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")  # a list item's mark at a line's start: -, *, •, "1." or "1)"
HEADING = re.compile(r"^\s*#{1,6}\s*")  # a Markdown heading's leading #s
RULE = re.compile(r"\s*([-*_]\s*){3,}")  # a Markdown rule: 3 or more -, * or _, spaces allowed
# One line of an IMAP LIST answer: (flags) "delimiter" name, the name quoted or not.
LIST_LINE = re.compile(rb'\(([^)]*)\) "[^"]*" "?([^"]+)"?$')

PARAGRAPH = '<p style="margin:0 0 14px">{}</p>'
LIST = '<ul style="margin:0 0 14px;padding-left:22px">{}</ul>'
SIGNED = (
    '<p style="margin:22px 0 0;padding-top:12px;border-top:1px solid #dddddd;color:#555555">'
    "Cordialement,<br><br><strong>Tarik</strong><br>{}</p>"
)
PAGE = (
    '<!doctype html><html><body style="margin:0;padding:24px;background:#ffffff">'
    '<div style="max-width:640px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222222">'
    "{}{}</div></body></html>"
)


def cert_pin():
    """The mail server's certificate pin (MAIL_CERT_SHA256, shared), "" for the usual check."""
    return setting("MAIL_CERT_SHA256", "").replace(":", "").strip().lower()


def tls_context():
    """Certificates verified as usual, or, with a pin, checked by check_pin once connected."""
    context = ssl.create_default_context()
    if cert_pin():
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # check_pin decides instead
    return context


def check_pin(sock):
    """With a pin, close the connection before anything is sent unless the server showed that certificate."""
    pin = cert_pin()
    if pin and hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest() != pin:
        sock.close()
        raise ssl.SSLError("mail server certificate does not match MAIL_CERT_SHA256")


@contextlib.contextmanager
def imap_session():
    """The current cabinet's mailbox over IMAP, logged in on the pinned (or verified) certificate, and closed on the
    way out whatever happened: a LOGOUT the server can no longer answer still closes the socket."""
    port = int(setting("IMAP_PORT", "993"))
    box = imaplib.IMAP4_SSL(setting("IMAP_HOST"), port, ssl_context=tls_context(), timeout=MAIL_TIMEOUT)
    try:
        check_pin(box.sock)
        box.login(setting("IMAP_USER"), setting("IMAP_PASSWORD"))
        yield box
    finally:
        try:
            box.logout()
        except Exception:  # LOGOUT failed before imaplib closed the socket: close it here
            with contextlib.suppress(OSError):
                box.shutdown()


def allowed_recipients():
    """The current cabinet's whitelist (MAIL_ALLOWED_RECIPIENTS), lowercased entries. Empty: nobody."""
    return [entry.strip().lower() for entry in setting("MAIL_ALLOWED_RECIPIENTS", "").split(",") if entry.strip()]


def recipient_allowed(address, allowed=None):
    """Is this address on the whitelist (``allowed``, by default the cabinet's)? Compared on the bare address,
    without the display name."""
    address = (getaddresses([address or ""])[0][1] or "").strip().lower()
    allowed = allowed_recipients() if allowed is None else allowed
    patterns = ["*" + entry if entry.startswith("@") else entry for entry in allowed]
    # "*" is the only wildcard (any text): the rest of the entry matches literally.
    return bool(address) and any(re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), address) for pattern in patterns)


_names = {}  # cabinet -> its name in Experio: read once it could be


def cabinet_name():
    """The current cabinet's name in Experio, for the signature ("" while Experio can't be reached: read again
    next time)."""
    if current() not in _names:
        try:
            _names[current()] = odoo_call("res.company", "read", [1], fields=["name"])[0]["name"]
        except Exception as error:
            log(f"cabinet_name: {error}")
            return ""
    return _names[current()]


def blocks(body):
    """The message as paragraphs and lists, cleaned of HTML tags and Markdown headings and rules:
    [("p", [lines]) | ("ul", [items])]."""
    found = []
    for line in strip_tags(body).replace("\r", "").split("\n"):
        line = HEADING.sub("", line).rstrip()
        if RULE.fullmatch(line) or not line.strip():  # a rule or a blank line ends a block
            found.append(None)
            continue
        kind, text = ("ul", BULLET.sub("", line)) if BULLET.match(line) else ("p", line.strip())
        if found and found[-1] and found[-1][0] == kind:
            found[-1][1].append(text)
        else:
            found.append((kind, [text]))
    return [block for block in found if block]


def spans(text, outside, inside):
    """``text`` with ``outside`` applied to what is not code and ``inside`` to each `code` span's content: what is
    in backticks (a file name) is never read as Markdown."""
    parts = CODE.split(text)  # even: outside code, odd: a code span's content
    return "".join(inside(part) if i % 2 else outside(part) for i, part in enumerate(parts))


def inline_text(text):
    """One line's Markdown as plain text: links as "label (address)", emphasis marks dropped, code in « »."""

    def outside(part):
        part = LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", part)
        return ITALIC.sub(r"\1", BOLD.sub(lambda m: m.group(1) or m.group(2), part))

    return spans(text, outside, lambda code: f"« {code} »")


def inline_html(text):
    """One line's Markdown as escaped HTML: links, bold and italics as tags, code as <code>."""

    def outside(part):
        part = html.escape(part, quote=False)
        part = LINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', part)
        part = BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", part)
        return ITALIC.sub(r"<em>\1</em>", part)

    return spans(text, outside, lambda code: CODE_HTML.format(html.escape(code, quote=False)))


def plain_block(kind, lines):
    """One block as plain text: a list's items with "- "."""
    return "\n".join(f"- {inline_text(line)}" if kind == "ul" else inline_text(line) for line in lines)


def html_block(kind, lines):
    """One block as HTML: a list, or a paragraph with its lines broken."""
    if kind == "ul":
        return LIST.format("".join(f"<li>{inline_html(item)}</li>" for item in lines))
    return PARAGRAPH.format("<br>".join(inline_html(line) for line in lines))


def render(body):
    """The email Tarik sends, as plain text and HTML: the same message in the cabinet's
    template, with its signature. No Markdown or HTML tag ever reaches the client raw."""
    parts = blocks(body)
    name = cabinet_name()
    signer = "Assistant comptable" + (f" | {name}" if name else "")
    text = "\n\n".join(plain_block(kind, lines) for kind, lines in parts)
    rendered = "".join(html_block(kind, lines) for kind, lines in parts)
    page = PAGE.format(rendered, SIGNED.format(html.escape(signer)))
    return text + "\n\n" + "\n".join(["Cordialement,", "", "Tarik", signer]), page


def sent_folder(box):
    """The mailbox's Sent folder, as the server flags it (special-use \\Sent), else "Sent"."""
    for line in box.list()[1] or []:
        found = LIST_LINE.match(line or b"")
        if found and b"\\Sent" in found.group(1):
            return found.group(2).decode()
    return "Sent"


def save_sent(message):
    """A copy of what Tarik sent, in the mailbox's Sent folder, read: SMTP keeps none, and the
    cabinet must see Tarik's replies there. The email is already sent: a failure here is only logged."""
    try:
        with imap_session() as box:
            box.append(f'"{sent_folder(box)}"', "(\\Seen)", imaplib.Time2Internaldate(time.time()), message.as_bytes())
    except Exception as error:
        log(f"save_sent: {error}")


def whitelisted(to):
    """The bare addresses of ``to`` (comma-separated); ToolError unless there is one and all are whitelisted."""
    recipients = [address for _, address in getaddresses([to or ""]) if address]
    if not recipients:
        raise ToolError("Aucun destinataire.")
    refused = [address for address in recipients if not recipient_allowed(address)]
    if refused:
        log(f"send_email refused: {refused} not whitelisted")
        raise ToolError(f"Email non envoye : destinataire hors de la liste autorisee ({', '.join(refused)}).")
    return recipients


def compose(sender, recipients, subject, body, in_reply_to, references):
    """The email (EmailMessage) from ``sender`` (its SMTP user) to ``recipients``, in its thread when it is a reply,
    as plain text and HTML."""
    message = EmailMessage()
    message["From"] = setting("SMTP_FROM", sender)
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)  # mail apps sort by it; the Sent copy gets no other
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join(filter(None, [references, in_reply_to]))
    text, page = render(body)
    message.set_content(text)
    message.add_alternative(page, subtype="html")
    return message


def smtp_send(host, user, message):
    """Send ``message`` through SMTP server ``host`` as ``user``: TLS from the start on port 465, else STARTTLS."""
    port, context = int(setting("SMTP_PORT", "465")), tls_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, context=context, timeout=MAIL_TIMEOUT)
    else:
        smtp = smtplib.SMTP(host, port, timeout=MAIL_TIMEOUT)
    with smtp:
        if port != 465:
            smtp.starttls(context=context)
        check_pin(smtp.sock)
        smtp.login(user, setting("SMTP_PASSWORD"))
        smtp.send_message(message)


@tool
def send_email(to: str, subject: str, body: str, in_reply_to: str = "", references: str = "") -> dict:
    """Send Tarik's email to ``to`` (one or more addresses, comma-separated), in the thread
    given by ``in_reply_to`` and ``references`` when it is a reply. Refused unless every
    recipient is on the whitelist."""
    recipients = whitelisted(to)
    host, user = setting("SMTP_HOST", ""), setting("SMTP_USER", "")
    if not host or not user:
        raise ToolError("Email non envoye : SMTP non configure.")
    message = compose(user, recipients, subject, body, in_reply_to, references)
    try:
        smtp_send(host, user, message)
    except Exception as error:
        log(f"send_email: {error}")
        raise ToolError("Email non envoye : le serveur d'envoi a refuse.") from None
    save_sent(message)
    return ok(f"Email envoye a {', '.join(recipients)}.", message_id=message["Message-ID"])
