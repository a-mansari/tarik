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
    with pytest.raises(ToolError, match="hors de la liste autorisee"):
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
    monkeypatch.setattr(sending, "current", lambda: "tarik_test_a")
    assert sending.cabinet_name() == ""
    assert sending.cabinet_name() == sending.cabinet_name() == "CABINET TEST"  # then kept: Experio asked once more


@pytest.mark.communication
def test_numbered_questions_stay_numbered(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    text, page = render("**Questions**\n1. Facture 12 : TVA ? oui / non\n2. Facture 13 : quel compte ?\n- autre")
    assert "1. Facture 12 : TVA ? oui / non\n2. Facture 13 : quel compte ?" in text and "- autre" in text
    assert "<ol" in page and "<li>Facture 13 : quel compte ?</li></ol>" in page
