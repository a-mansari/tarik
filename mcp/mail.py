"""Tarik's emails: replies go out through SMTP, only to whitelisted addresses.

MAIL_ALLOWED_RECIPIENTS lists who may receive an email from Tarik: full addresses, "@domain"
for a whole domain, or patterns with "*" ("test.*@experio.ma"; "*" alone lets anyone through).
Empty means nobody: no email leaves until someone is listed.

MAIL_CERT_SHA256 pins the mail server's certificate when it has no certificate a known
authority signed (mailcow's own, self-signed): only that exact certificate is accepted, so
the connection stays safe from interception. Empty: the usual verification.
"""

import functools
import hashlib
import html
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid

from mcp.server.mcpserver.exceptions import ToolError

from app import ok, server

CERT_SHA256 = os.getenv("MAIL_CERT_SHA256", "").replace(":", "").strip().lower()


def tls_context():
    """Certificates verified as usual, or, with a pin, checked by check_pin once connected."""
    context = ssl.create_default_context()
    if CERT_SHA256:
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # check_pin decides instead
    return context


def check_pin(sock):
    """With a pin, close the connection before anything is sent unless the server showed that certificate."""
    if CERT_SHA256 and hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest() != CERT_SHA256:
        sock.close()
        raise ssl.SSLError("mail server certificate does not match MAIL_CERT_SHA256")


ALLOWED_RECIPIENTS = [entry.strip().lower() for entry in os.getenv("MAIL_ALLOWED_RECIPIENTS", "").split(",") if entry.strip()]


def recipient_allowed(address, allowed=None):
    """Is this address on the whitelist? Compared on the bare address, without the display name."""
    address = (getaddresses([address or ""])[0][1] or "").strip().lower()
    allowed = ALLOWED_RECIPIENTS if allowed is None else allowed
    # "*" is the only wildcard: the rest of the entry matches literally.
    patterns = ["*" + entry if entry.startswith("@") else entry for entry in allowed]
    return bool(address) and any(re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), address) for pattern in patterns)


TAG = re.compile(r"</?[a-zA-Z][^<>]*>")  # an HTML tag, not "a < b"
BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


@functools.lru_cache(maxsize=1)
def cabinet_name():
    """The cabinet's name in Experio, for the signature ("" when Experio can't be reached)."""
    try:
        from experio import odoo_call
        return odoo_call("res.company", "read", [1], fields=["name"])[0]["name"]
    except Exception as error:
        print(f"cabinet_name: {error}", flush=True)
        return ""


def blocks(body):
    """The message as paragraphs and lists, cleaned of HTML tags and Markdown headings and rules:
    [("p", [lines]) | ("ul", [items])]."""
    found = []
    for line in TAG.sub("", body or "").replace("\r", "").split("\n"):
        line = re.sub(r"^\s*#{1,6}\s*", "", line).rstrip()
        if re.fullmatch(r"\s*([-*_]\s*){3,}", line) or not line.strip():  # a rule or a blank line ends a block
            found.append(None)
            continue
        kind, text = ("ul", BULLET.sub("", line)) if BULLET.match(line) else ("p", line.strip())
        if found and found[-1] and found[-1][0] == kind:
            found[-1][1].append(text)
        else:
            found.append((kind, [text]))
    return [block for block in found if block]


def inline_text(text):
    text = LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    return ITALIC.sub(r"\1", CODE.sub(r"\1", text))


def inline_html(text):
    text = html.escape(text, quote=False)
    text = LINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', text)
    text = BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    return ITALIC.sub(r"<em>\1</em>", CODE.sub(r"\1", text))


def render(body):
    """The email Tarik sends, as plain text and HTML: the same message in the cabinet's
    template, with its signature. No Markdown or HTML tag ever reaches the client raw."""
    parts = blocks(body)
    signature = ["Cordialement,", "", "Tarik", "Assistant comptable" + (f" | {cabinet_name()}" if cabinet_name() else "")]
    text = "\n\n".join("\n".join((f"- {inline_text(line)}" if kind == "ul" else inline_text(line)) for line in lines)
                       for kind, lines in parts)
    rendered = "".join(f"<ul style=\"margin:0 0 14px;padding-left:22px\">{''.join(f'<li>{inline_html(item)}</li>' for item in lines)}</ul>"
                       if kind == "ul" else f"<p style=\"margin:0 0 14px\">{'<br>'.join(inline_html(line) for line in lines)}</p>"
                       for kind, lines in parts)
    signed = ("<p style=\"margin:22px 0 0;padding-top:12px;border-top:1px solid #dddddd;color:#555555\">"
              f"Cordialement,<br><br><strong>Tarik</strong><br>{html.escape(signature[3])}</p>")
    page = ("<!doctype html><html><body style=\"margin:0;padding:24px;background:#ffffff\">"
            "<div style=\"max-width:640px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222222\">"
            f"{rendered}{signed}</div></body></html>")
    return text + "\n\n" + "\n".join(signature), page


@server.tool()
def send_email(to: str, subject: str, body: str, in_reply_to: str = "", references: str = "") -> dict:
    """Send Tarik's email to ``to`` (one or more addresses, comma-separated), in the thread
    given by ``in_reply_to`` and ``references`` when it is a reply. Refused unless every
    recipient is on the whitelist."""
    recipients = [address for _, address in getaddresses([to or ""]) if address]
    if not recipients:
        raise ToolError("Aucun destinataire.")
    refused = [address for address in recipients if not recipient_allowed(address)]
    if refused:
        print(f"send_email refused: {refused} not whitelisted", flush=True)
        raise ToolError(f"Email non envoye : destinataire hors de la liste autorisee ({', '.join(refused)}).")
    host, user = os.getenv("SMTP_HOST", ""), os.getenv("SMTP_USER", "")
    if not host or not user:
        raise ToolError("Email non envoye : SMTP non configure.")
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM") or user
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join(filter(None, [references, in_reply_to]))
    text, page = render(body)
    message.set_content(text)
    message.add_alternative(page, subtype="html")
    port, context = int(os.getenv("SMTP_PORT") or 465), tls_context()
    try:
        with (smtplib.SMTP_SSL(host, port, context=context, timeout=60) if port == 465 else smtplib.SMTP(host, port, timeout=60)) as smtp:
            if port != 465:
                smtp.starttls(context=context)
            check_pin(smtp.sock)
            smtp.login(user, os.getenv("SMTP_PASSWORD", ""))
            smtp.send_message(message)
    except Exception as error:
        print(f"send_email: {error}", flush=True)
        raise ToolError("Email non envoye : le serveur d'envoi a refuse.") from None
    return ok(f"Email envoye a {', '.join(recipients)}.", message_id=message["Message-ID"])
