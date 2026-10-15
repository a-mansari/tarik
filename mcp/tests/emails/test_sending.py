"""Outbound emails: the whitelist, and the template (no raw Markdown or HTML, one signature)."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.emails import sending
from tarik.emails.sending import recipient_allowed, render, send_email


@pytest.mark.security
def test_the_whitelist():  # exact addresses, @domain, * patterns; nobody when empty
    allowed = ["test@example.com", "@cabinet.example"]
    assert recipient_allowed("Test <TEST@example.com>", allowed) and recipient_allowed("x@cabinet.example", allowed)
    assert not recipient_allowed("client@other.example", allowed) and not recipient_allowed("x@evilcabinet.example", allowed)
    assert not recipient_allowed("test@example.com", []) and not recipient_allowed("", allowed)
    assert recipient_allowed("test.ali@experio.ma", ["test.*@experio.ma"]) and not recipient_allowed("ali@experio.ma", ["test.*@experio.ma"])
    assert recipient_allowed("a@sub.cabinet.example", ["*@*.cabinet.example"]) and not recipient_allowed("a@cabinet.example", ["*@*.cabinet.example"])
    assert recipient_allowed("anyone@anywhere.example", ["*"])
    assert not recipient_allowed("testXali@experio.ma", ["test.ali@experio.ma"])  # "." is literal, not a wildcard


@pytest.mark.security
def test_an_email_outside_the_whitelist_is_refused_before_smtp(monkeypatch):
    monkeypatch.setattr(sending, "allowed_recipients", lambda: ["test@example.com"])  # whatever the container's own list
    with pytest.raises(ToolError, match="hors de la liste autorisee"):
        send_email("client@other.example", "s", "b")


@pytest.mark.communication
def test_the_template(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "CABINET TEST")
    text, page = render("## Récapitulatif\n\nVoici le **dossier** ALPHA :\n- création : ok\n* import : `7197` lignes\n\n"
                        "<b>Merci</b>, voir [Experio](https://experio.ma).\n---\nA < B et *important*")
    for raw in ("**", "##", "`", "<b>", "</b>", "](", "---"):
        assert raw not in text, raw
    assert "Voici le dossier ALPHA :" in text and "- création : ok" in text and "- import : 7197 lignes" in text
    assert "Experio (https://experio.ma)" in text and "A < B et important" in text
    assert text.endswith("Cordialement,\n\nTarik\nAssistant comptable | CABINET TEST")
    assert "<strong>dossier</strong>" in page and "<li>création : ok</li>" in page and "<b>" not in page and "**" not in page
    assert '<a href="https://experio.ma">Experio</a>' in page and "A &lt; B" in page and "<em>important</em>" in page


@pytest.mark.communication
def test_ordinary_underscores_and_stars_stay(monkeypatch):
    monkeypatch.setattr(sending, "cabinet_name", lambda: "")
    assert "fichier_2026_mars.pdf" in render("fichier_2026_mars.pdf")[0] and "4411 * 2" in render("4411 * 2")[0]


class FakeBox:
    def __init__(self, folders):
        self.folders = folders

    def list(self):
        return "OK", self.folders


@pytest.mark.technical
def test_the_sent_folder_is_the_one_the_server_flags():
    assert sending.sent_folder(FakeBox([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Envoyes"'])) == "Envoyes"
    assert sending.sent_folder(FakeBox([b'(\\HasNoChildren) "/" "INBOX"'])) == "Sent"  # none flagged: the usual name
