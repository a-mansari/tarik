"""Outbound emails: the whitelist, and the template (no raw Markdown or HTML, one signature)."""

from contextlib import nullcontext
from email.utils import parsedate_to_datetime

import pytest

from engine.tools.core.app import ToolError
from engine.tools.emails import sending
from engine.tools.emails.sending import recipient_allowed, render, send_email


@pytest.mark.security
def test_the_whitelist():  # exact addresses, @domain, * patterns; nobody when empty
    allowed = ["test@example.com", "@cabinet.example"]
    assert recipient_allowed("Test <TEST@example.com>", allowed) and recipient_allowed("x@cabinet.example", allowed)
    assert not recipient_allowed("client@other.example", allowed) and not recipient_allowed(
        "x@evilcabinet.example", allowed
    )
    assert not recipient_allowed("test@example.com", []) and not recipient_allowed("", allowed)
    assert recipient_allowed("test.ali@experio.ma", ["test.*@experio.ma"]) and not recipient_allowed(
        "ali@experio.ma", ["test.*@experio.ma"]
    )
    assert recipient_allowed("a@sub.cabinet.example", ["*@*.cabinet.example"]) and not recipient_allowed(
        "a@cabinet.example", ["*@*.cabinet.example"]
    )
    assert recipient_allowed("anyone@anywhere.example", ["*"])
    assert not recipient_allowed("testXali@experio.ma", ["test.ali@experio.ma"])  # "." is literal, not a wildcard


@pytest.mark.security
def test_an_email_outside_the_whitelist_is_refused_before_smtp(monkeypatch):
    monkeypatch.setattr(
        sending, "allowed_recipients", lambda: ["test@example.com"]
    )  # whatever the container's own list
    with pytest.raises(ToolError, match="hors de la liste autorisée"):
        send_email("client@other.example", "s", "b")


@pytest.mark.communication
def test_the_template(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "CABINET TEST")
    text, page = render(
        "## Récapitulatif\n\nVoici le **dossier** ALPHA :\n- création : ok\n* import : `7197` lignes\n\n"
        "<b>Merci</b>, voir [Experio](https://experio.ma).\n---\nA < B et *important*"
    )
    for raw in ("**", "##", "`", "<b>", "</b>", "](", "---"):
        assert raw not in text, raw
    assert "Voici le dossier ALPHA :" in text and "- création : ok" in text and "- import : « 7197 » lignes" in text
    assert "Experio (https://experio.ma)" in text and "A < B et important" in text
    assert text.endswith("Cordialement,\n\nTarik\nAssistant comptable | CABINET TEST")
    assert (
        "<strong>dossier</strong>" in page
        and "<li>création : ok</li>" in page
        and "<b>" not in page
        and "**" not in page
    )
    assert '<a href="https://experio.ma">Experio</a>' in page and "A &lt; B" in page and "<em>important</em>" in page
    assert ">7197</code> lignes" in page


@pytest.mark.communication
def test_an_email_is_dated_and_threaded(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "CABINET TEST")
    monkeypatch.setattr(sending, "setting", lambda name, default="": default)
    message = sending.compose("tarik@cabinet.ma", ["chef@cabinet.ma"], "Re: Factures", "Bonjour", "<m1@x>", "")
    assert parsedate_to_datetime(message["Date"]).tzinfo is not None  # mail apps sort the thread by it
    assert message["In-Reply-To"] == "<m1@x>" and message["References"] == "<m1@x>"


@pytest.mark.communication
def test_ordinary_underscores_and_stars_stay(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    assert "fichier_2026_mars.pdf" in render("fichier_2026_mars.pdf")[0] and "4411 * 2" in render("4411 * 2")[0]


@pytest.mark.communication
def test_a_file_name_keeps_its_double_underscores_and_stands_out(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    name = "invoice-purchase__65582__document_pdf.pdf"
    for body in (f"{name} : relevé importé.", f"`{name}` : relevé importé."):
        text, page = render(body)
        assert name in text and name in page, body
    text, page = render(f"`{name}` et `mon fichier (2).pdf` : relevés importés, __vraiment__.")
    assert (
        f"« {name} »" in text
        and "« mon fichier (2).pdf »" in text
        and "vraiment" in text
        and "__vraiment__" not in text
    )
    assert f">{name}</code>" in page and ">mon fichier (2).pdf</code>" in page and "<strong>vraiment</strong>" in page


class FakeBox:
    def __init__(self, folders):
        self.folders = folders

    def list(self):
        return "OK", self.folders


@pytest.mark.technical
def test_the_sent_folder_is_the_one_the_server_flags():
    assert (
        sending.sent_folder(FakeBox([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Envoyes"']))
        == "Envoyes"
    )
    assert sending.sent_folder(FakeBox([b'(\\HasNoChildren) "/" "INBOX"'])) == "Sent"  # none flagged: the usual name


class Imap:
    """An IMAP connection whose login (or LOGOUT) fails as asked; ``closed`` once its socket is."""

    def __init__(self, *args, fail_login=False, fail_logout=False, **kwargs):
        self.sock, self.closed, self.fail_login, self.fail_logout = None, False, fail_login, fail_logout

    def login(self, user, password):
        if self.fail_login:
            raise OSError("wrong password")

    def logout(self):
        if self.fail_logout:
            raise OSError("connection reset")  # imaplib raises before it closes the socket
        self.closed = True

    def shutdown(self):
        self.closed = True


@pytest.mark.technical
@pytest.mark.parametrize(
    "failing", [{}, {"fail_login": True}, {"fail_logout": True}, {"fail_login": True, "fail_logout": True}]
)
@pytest.mark.parametrize("work_fails", [False, True])
def test_an_imap_connection_is_always_closed(monkeypatch, failing, work_fails):
    opened = []
    monkeypatch.setattr(sending.imaplib, "IMAP4_SSL", lambda *a, **k: opened.append(Imap(**failing)) or opened[-1])
    monkeypatch.setattr(sending, "setting", lambda key, default="": "993" if key == "IMAP_PORT" else "x")
    monkeypatch.setattr(sending, "check_pin", lambda sock: None)
    raised = pytest.raises((OSError, RuntimeError)) if failing.get("fail_login") or work_fails else nullcontext()
    with raised, sending.imap_session():
        if work_fails:
            raise RuntimeError("the work on the mailbox failed")
    assert opened[0].closed


@pytest.mark.technical
def test_a_cabinet_name_experio_could_not_give_is_asked_again(monkeypatch):
    answers = [ConnectionError("Experio down"), [{"name": "CABINET TEST"}]]

    def odoo_call(*args, **kwargs):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(sending, "_names", {})
    monkeypatch.setattr(sending, "odoo_call", odoo_call)
    monkeypatch.setattr(sending, "main_company_id", lambda: 1)
    monkeypatch.setattr(sending, "current", lambda: "tarik_test_a")
    assert sending.cabinet_name() == ""
    assert sending.cabinet_name() == sending.cabinet_name() == "CABINET TEST"  # then kept: Experio asked once more


@pytest.mark.communication
def test_numbered_questions_stay_numbered(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    text, page = render("**Questions**\n1. Facture 12 : TVA ? oui / non\n2. Facture 13 : quel compte ?\n- autre")
    assert "1. Facture 12 : TVA ? oui / non\n2. Facture 13 : quel compte ?" in text and "- autre" in text
    assert "<ol" in page and "<li>Facture 13 : quel compte ?</li></ol>" in page


@pytest.mark.communication
def test_an_invoice_s_outcomes_stay_listed_under_it(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    text, page = render(
        "**Fait**\n- Facture 21 :\n  - tiers `ATLAS`\n  - comptabilisée.\n- Facture 22 : comptabilisée."
    )
    assert "- Facture 21 :\n  - tiers « ATLAS »\n  - comptabilisée.\n- Facture 22 : comptabilisée." in text
    assert "<li>Facture 21 :<ul" in page and "<li>comptabilisée.</li></ul></li><li>Facture 22" in page
    assert "max-width:960px;min-width:320px" in page  # as wide as the window, within bounds


@pytest.mark.functionality
def test_the_files_tarik_made_go_with_the_email(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    monkeypatch.setattr(sending, "setting", lambda name, default="": default)
    files = [("FEC_TARIK_TEST_2026.txt", "text/plain", b"Code_Journal;Libelle")]
    message = sending.compose("tarik@cabinet.ma", ["chef@cabinet.ma"], "Re: Export", "Voici l'export.", "", "", files)
    attached = list(message.iter_attachments())
    assert [part.get_filename() for part in attached] == ["FEC_TARIK_TEST_2026.txt"]
    assert attached[0].get_content() == "Code_Journal;Libelle"


SMTP = {"SMTP_HOST": "smtp.cabinet.ma", "SMTP_USER": "tarik@cabinet.ma", "MAIL_ALLOWED_RECIPIENTS": "@cabinet.ma"}


def mailer(monkeypatch, settings=None, refusal=None):
    sent = []

    def smtp_send(host, user, message):
        if refusal:
            raise refusal
        sent.append(message)

    values = SMTP if settings is None else settings
    monkeypatch.setattr(sending, "setting", lambda name, default=None: values.get(name) or default)
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    monkeypatch.setattr(sending, "smtp_send", smtp_send)
    monkeypatch.setattr(sending, "save_sent", lambda message: None)
    return sent


def unsent(session_id):
    from engine.tools.core.cabinets import cabinet_db
    from engine.tools.core.queries import Q

    with cabinet_db() as db:
        return [row[1] for row in db.execute(Q.outbox_unsent, (session_id,))]


@pytest.mark.functionality
def test_the_conversation_files_go_once_with_the_next_email(db_cabinet, monkeypatch):
    from engine.tools.core import outbox

    sent = mailer(monkeypatch)
    outbox.keep_file("s1", "export.xlsx", b"xlsx")
    outbox.keep_file("other", "tva.xml", b"<tva/>")
    result = send_email("chef@cabinet.ma", "Re: Export", "Voici l'export.", attach_session="s1")
    assert result["message_id"] == sent[0]["Message-ID"]
    assert [part.get_filename() for part in sent[0].iter_attachments()] == ["export.xlsx"]
    assert unsent("s1") == [] and unsent("other") == ["tva.xml"]
    send_email("chef@cabinet.ma", "Re: Export", "Encore.", attach_session="s1")
    assert list(sent[1].iter_attachments()) == []  # never sent twice


@pytest.mark.communication
def test_an_email_the_server_refuses_keeps_its_files_for_the_next(db_cabinet, monkeypatch):
    from engine.tools.core import outbox

    mailer(monkeypatch, refusal=OSError("550 refused"))
    outbox.keep_file("s1", "export.xlsx", b"xlsx")
    with pytest.raises(ToolError, match="le serveur d'envoi a refusé"):
        send_email("chef@cabinet.ma", "Re: Export", "Voici l'export.", attach_session="s1")
    assert unsent("s1") == ["export.xlsx"]


@pytest.mark.communication
@pytest.mark.parametrize(
    ("to", "settings", "said"),
    [
        ("", SMTP, "Aucun destinataire"),
        ("chef@cabinet.ma", {"MAIL_ALLOWED_RECIPIENTS": "@cabinet.ma"}, "SMTP non configuré"),
    ],
)
def test_an_email_without_recipient_or_smtp_is_refused(monkeypatch, to, settings, said):
    sent = mailer(monkeypatch, settings)
    with pytest.raises(ToolError, match=said):
        send_email(to, "s", "b")
    assert sent == []


@pytest.mark.technical
def test_a_sent_copy_the_mailbox_refuses_is_only_logged(monkeypatch):
    def broken():
        raise OSError("imap down")

    monkeypatch.setattr(sending, "imap_session", broken)
    sending.save_sent(sending.EmailMessage())  # the email is already sent: nothing raised


class FakeSmtp:
    made = []

    def __init__(self, host, port, context=None, timeout=None):
        self.calls, self.sock = [("open", host, port)], object()
        FakeSmtp.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.calls.append("send")


@pytest.mark.security
@pytest.mark.parametrize(("port", "tls"), [("465", []), ("587", ["starttls"])])
def test_smtp_is_encrypted_from_the_start_on_465_else_by_starttls_before_login(monkeypatch, port, tls):
    settings = {"SMTP_PORT": port, "SMTP_PASSWORD": "pw"}
    monkeypatch.setattr(sending, "setting", lambda name, default=None: settings.get(name, default))
    monkeypatch.setattr(sending.smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(sending.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(sending, "check_pin", lambda sock: None)
    FakeSmtp.made.clear()
    sending.smtp_send("smtp.cabinet.ma", "tarik@cabinet.ma", None)
    assert FakeSmtp.made[0].calls[1:] == [*tls, ("login", "tarik@cabinet.ma", "pw"), "send"]


@pytest.mark.security
def test_a_certificate_other_than_the_pinned_one_closes_the_connection(monkeypatch):
    class Sock:
        closed = False

        def getpeercert(self, binary_form):
            return b"another certificate"

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        sending, "setting", lambda name, default=None: "AB:CD" if name == "MAIL_CERT_SHA256" else default
    )
    sock = Sock()
    with pytest.raises(sending.ssl.SSLError, match="does not match"):
        sending.check_pin(sock)
    assert sock.closed
