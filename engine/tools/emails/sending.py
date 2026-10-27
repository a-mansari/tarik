"""Send Tarik's emails over SMTP, only to whitelisted addresses, and open IMAP sessions to the mailbox.

MAIL_ALLOWED_RECIPIENTS lists who may receive an email from Tarik: full addresses, "@domain" for a whole domain, or
patterns with "*" ("test.*@experio.ma"; "*" alone lets anyone through). Empty means nobody: no email leaves until
someone is listed.

MAIL_CERT_SHA256 pins the mail server's certificate when no known authority signed it (mailcow's own, self-signed):
only that exact certificate is accepted, which keeps the connection safe from interception. Empty: the usual
certificate verification."""

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

from engine.tools.core.app import ToolError, log, ok, tool
from engine.tools.core.cabinets import cabinet_db, current, setting
from engine.tools.core.experio import main_company_id, odoo_call
from engine.tools.core.queries import Q
from engine.tools.core.words import strip_tags

MAIL_TIMEOUT = 60  # seconds before the mail server is given up

BOLD = re.compile(r"\*\*(.+?)\*\*|(?<!\w)__(.+?)__(?!\w)")  # **text** or __text__, never inside a word (a__b__c)
# *text*: single stars, not inside a word nor next to another star, the text not starting or ending with a space.
ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
CODE = re.compile(r"`([^`\n]+)`")  # `text` on one line: a file name, kept as written
CODE_HTML = '<code style="background:#f2f2f2;padding:1px 4px;border-radius:3px">{}</code>'
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")  # [label](http(s) address)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")  # a list item's mark at a line's start: -, *, •, "1." or "1)"
NUMBERED = re.compile(r"^\s*\d+[.)]\s+")  # a numbered item: kept numbered, the client answers « 1 oui, 2 non »
HEADING = re.compile(r"^\s*#{1,6}\s*")  # a Markdown heading's leading #s
RULE = re.compile(r"\s*([-*_]\s*){3,}")  # a Markdown rule: 3 or more -, * or _, spaces allowed
# One line of an IMAP LIST answer: (flags) "delimiter" name, the name quoted or not.
LIST_LINE = re.compile(rb'\(([^)]*)\) "[^"]*" "?([^"]+)"?$')

PARAGRAPH = '<p style="margin:0 0 14px">{}</p>'
LIST = '<ul style="margin:0 0 14px;padding-left:22px">{}</ul>'
NUMBERED_LIST = '<ol style="margin:0 0 14px;padding-left:22px">{}</ol>'
SUBLIST = '<ul style="margin:2px 0 0;padding-left:22px">{}</ul>'  # an item's own items, indented under it
SIGNED = (
    '<p style="margin:22px 0 0;padding-top:12px;border-top:1px solid #dddddd;color:#555555">'
    "Cordialement,<br><br><strong>Tarik</strong><br>{}</p>"
)
PAGE = (
    '<!doctype html><html><body style="margin:0;padding:24px;background:#ffffff">'
    # As wide as the reader's window, up to 960px (longer lines read badly), never squeezed under 320px.
    '<div style="max-width:960px;min-width:320px;font-family:Arial,Helvetica,sans-serif;font-size:14px;'
    'line-height:1.5;color:#222222">'
    "{}{}</div></body></html>"
)


def cert_pin():
    """Return the mail server's certificate pin (MAIL_CERT_SHA256, lowercase hex without colons); "" for the usual
    check."""
    return setting("MAIL_CERT_SHA256", "").replace(":", "").strip().lower()


def tls_context():
    """Return the TLS context: usual certificate verification, or none when a pin is set (check_pin checks after
    connecting)."""
    context = ssl.create_default_context()
    if cert_pin():
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # check_pin decides instead
    return context


def check_pin(sock):
    """Close ``sock`` and raise ssl.SSLError when a pin is set and the server's certificate does not match it.

    Called right after connecting, before anything is sent."""
    pin = cert_pin()
    if pin and hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest() != pin:
        sock.close()
        raise ssl.SSLError("mail server certificate does not match MAIL_CERT_SHA256")


@contextlib.contextmanager
def imap_session():
    """Context manager: yield the current cabinet's IMAP connection, logged in after the certificate check.

    The connection is closed on the way out whatever happened: when LOGOUT fails, the socket is shut down directly."""
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
    """Return the current cabinet's whitelist entries (MAIL_ALLOWED_RECIPIENTS), lowercased; empty means nobody."""
    return [entry.strip().lower() for entry in setting("MAIL_ALLOWED_RECIPIENTS", "").split(",") if entry.strip()]


def recipient_allowed(address, allowed=None):
    """True when ``address`` is on the whitelist (``allowed``, default the cabinet's).

    The bare address is compared, without the display name. "@domain" matches the whole domain and "*" matches any
    text; the rest of an entry matches literally."""
    address = (getaddresses([address or ""])[0][1] or "").strip().lower()
    allowed = allowed_recipients() if allowed is None else allowed
    patterns = ["*" + entry if entry.startswith("@") else entry for entry in allowed]
    # "*" is the only wildcard (any text): the rest of the entry matches literally.
    return bool(address) and any(re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), address) for pattern in patterns)


_names = {}  # cabinet -> its name in Experio: read once it could be


def cabinet_name():
    """Return the current cabinet's name in Experio (base.main_company), for the signature.

    Cached per cabinet once read; "" while Experio cannot be reached (read again next time)."""
    if current() not in _names:
        try:
            _names[current()] = odoo_call("res.company", "read", [main_company_id()], fields=["name"])[0]["name"]
        except Exception as error:
            log(f"cabinet_name: {error}")
            return ""
    return _names[current()]


def block_kind(line):
    """Return the kind of block ``line`` belongs to: "ol" (numbered item), "ul" (list item) or "p" (paragraph)."""
    if NUMBERED.match(line):
        return "ol"
    if BULLET.match(line):
        return "ul"
    return "p"


def blocks(body):
    """Split ``body`` into paragraphs and lists, with HTML tags and Markdown headings and rules removed.

    Returns [(kind, items)]: ("p", [lines]), or ("ul" | "ol", [(text, [sub-items])]). A list item indented under
    another becomes its sub-item (one level only). A blank line or a rule ends a block."""
    found = []
    for line in strip_tags(body).replace("\r", "").split("\n"):
        line = HEADING.sub("", line).rstrip()
        if RULE.fullmatch(line) or not line.strip():  # a rule or a blank line ends a block
            found.append(None)
            continue
        kind = block_kind(line)
        text = BULLET.sub("", line) if kind != "p" else line.strip()
        listing = found and found[-1] and found[-1][0] != "p"
        if kind != "p" and listing and line[:2] == "  ":  # indented under the list's last item: its own item
            found[-1][1][-1][1].append(text)
            continue
        if kind != "p":
            text = (text, [])
        if found and found[-1] and found[-1][0] == kind:
            found[-1][1].append(text)
        else:
            found.append((kind, [text]))
    return [block for block in found if block]


def spans(text, outside, inside):
    """Return ``text`` with function ``outside`` applied to the parts outside backticks and ``inside`` to each `code`
    span's content.

    Text in backticks (a file name) is never read as Markdown."""
    parts = CODE.split(text)  # even: outside code, odd: a code span's content
    return "".join(inside(part) if i % 2 else outside(part) for i, part in enumerate(parts))


def inline_text(text):
    """Return one line's Markdown as plain text: links as "label (address)", emphasis marks dropped, code in « »."""

    def outside(part):
        part = LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", part)
        return ITALIC.sub(r"\1", BOLD.sub(lambda m: m.group(1) or m.group(2), part))

    return spans(text, outside, lambda code: f"« {code} »")


def inline_html(text):
    """Return one line's Markdown as escaped HTML: links, bold and italics as tags, code as <code>."""

    def outside(part):
        part = html.escape(part, quote=False)
        part = LINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', part)
        part = BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", part)
        return ITALIC.sub(r"<em>\1</em>", part)

    return spans(text, outside, lambda code: CODE_HTML.format(html.escape(code, quote=False)))


def plain_block(kind, lines):
    """Return one block (``kind``, ``lines`` from blocks) as plain text.

    List items start with "- ", numbered items with their number; sub-items are indented under their item."""
    if kind in ("ul", "ol"):
        shown = []
        for i, (text, subs) in enumerate(lines, 1):
            shown.append(f"{f'{i}.' if kind == 'ol' else '-'} {inline_text(text)}")
            shown += [f"  - {inline_text(sub)}" for sub in subs]
        return "\n".join(shown)
    return "\n".join(inline_text(line) for line in lines)


def html_block(kind, lines):
    """Return one block (``kind``, ``lines`` from blocks) as HTML: a list, or a paragraph with <br> between its lines.
    Return one block (``kind``, ``lines`` from blocks) as HTML: a list, or a paragraph with <br> between its lines."""
    if kind in ("ul", "ol"):
        frame = LIST if kind == "ul" else NUMBERED_LIST
        return frame.format("".join(f"<li>{inline_html(text)}{sublist(subs)}</li>" for text, subs in lines))
    return PARAGRAPH.format("<br>".join(inline_html(line) for line in lines))


def sublist(items):
    """Return an item's sub-items as an HTML list, "" when it has none."""
    return SUBLIST.format("".join(f"<li>{inline_html(item)}</li>" for item in items)) if items else ""


def plain(body):
    """Return Tarik's Markdown ``body`` as the plain text its email shows (render), without the signature."""
    return "\n\n".join(plain_block(kind, lines) for kind, lines in blocks(body))


def render(body):
    """Return the email Tarik sends as (plain text, HTML page), both signed with the cabinet's name.

    The same message in both forms, in the cabinet's template: no Markdown or HTML tag ever reaches the client raw."""
    parts = blocks(body)
    name = cabinet_name()
    signer = "Assistant comptable" + (f" | {name}" if name else "")
    text = plain(body)
    rendered = "".join(html_block(kind, lines) for kind, lines in parts)
    page = PAGE.format(rendered, SIGNED.format(html.escape(signer)))
    return text + "\n\n" + "\n".join(["Cordialement,", "", "Tarik", signer]), page


def sent_folder(box):
    """Return the name of the mailbox folder the server flags as Sent (IMAP special-use), else "Sent"."""
    for line in box.list()[1] or []:
        found = LIST_LINE.match(line or b"")
        if found and b"\\Sent" in found.group(1):
            return found.group(2).decode()
    return "Sent"


def save_sent(message):
    """Save a read copy of ``message`` in the mailbox's Sent folder.

    SMTP keeps no copy, and the cabinet must see Tarik's replies there. The email is already sent, so a failure here
    is only logged."""
    try:
        with imap_session() as box:
            box.append(f'"{sent_folder(box)}"', "(\\Seen)", imaplib.Time2Internaldate(time.time()), message.as_bytes())
    except Exception as error:
        log(f"save_sent: {error}")


def whitelisted(to):
    """Return the bare addresses in ``to`` (comma-separated).

    Raises:
        ToolError: no address, or one of them is not whitelisted."""
    recipients = [address for _, address in getaddresses([to or ""]) if address]
    if not recipients:
        raise ToolError("Aucun destinataire.")
    refused = [address for address in recipients if not recipient_allowed(address)]
    if refused:
        log(f"send_email refused: {refused} not whitelisted")
        raise ToolError(f"Email non envoye : destinataire hors de la liste autorisee ({', '.join(refused)}).")
    return recipients


def compose(sender, recipients, subject, body, in_reply_to, references, files=()):
    """Return the email (EmailMessage) from ``sender`` (the SMTP user) to ``recipients``, as plain text and HTML.

    Args:
        sender: the SMTP user; From is SMTP_FROM when set, and the Message-ID uses this address's domain.
        recipients: list of bare addresses.
        in_reply_to, references: the thread's message ids; when ``in_reply_to`` is set, the email is a reply in that
            thread.
        files: [(name, mime type, bytes)] to attach."""
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
    for name, mime, data in files:
        main, _, sub = (mime or "application/octet-stream").partition("/")
        message.add_attachment(bytes(data), maintype=main, subtype=sub or "octet-stream", filename=name)
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
def send_email(
    to: str, subject: str, body: str, in_reply_to: str = "", references: str = "", attach_session: str = ""
) -> dict:
    """Send Tarik's email to ``to`` (one or more addresses, comma-separated) and return an ok result with message_id.

    Args:
        in_reply_to, references: the thread's message ids, when the email is a reply.
        attach_session: a conversation id; the files Tarik made for it and has not sent yet (an export) are attached
            and then marked sent.
    Raises:
        ToolError: a recipient is not whitelisted, SMTP is not configured, or the SMTP server refused the email."""
    recipients = whitelisted(to)
    host, user = setting("SMTP_HOST", ""), setting("SMTP_USER", "")
    if not host or not user:
        raise ToolError("Email non envoye : SMTP non configure.")
    outbox = []
    if attach_session:
        with cabinet_db() as db:
            outbox = db.execute(Q.outbox_unsent, (attach_session,)).fetchall()
    files = [(name, mime, data) for _, name, mime, data in outbox]
    message = compose(user, recipients, subject, body, in_reply_to, references, files)
    try:
        smtp_send(host, user, message)
    except Exception as error:
        log(f"send_email: {error}")
        raise ToolError("Email non envoye : le serveur d'envoi a refuse.") from None
    if outbox:
        with cabinet_db() as db:
            db.execute(Q.mark_outbox_sent, ([file_id for file_id, *_ in outbox],))
    save_sent(message)
    return ok(f"Email envoye a {', '.join(recipients)}.", message_id=message["Message-ID"])
