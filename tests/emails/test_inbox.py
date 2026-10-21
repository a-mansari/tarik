"""Incoming emails: the client's own words, their files, automatic replies."""

from email.message import EmailMessage

import pytest

from tarik.emails.inbox import attachments, automatic, client_text


@pytest.mark.functionality
def test_the_client_text_and_files():
    mail = EmailMessage()
    mail["From"], mail["Subject"] = "Client <client@example.com>", "Factures"
    mail.set_content(
        "Bonjour,\nci-joint nos factures.\n\n"
        "Le lun. 13 oct. 2026, Tarik <tarik@example.com> a écrit :\n> Envoyez-moi vos factures\n"
    )
    mail.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="facture.pdf")
    mail.add_attachment(b"PNG", maintype="image", subtype="png", filename="logo.png", disposition="inline")
    mail.add_attachment(b"zip", maintype="application", subtype="zip", filename="archive.zip")
    assert client_text(mail) == "Bonjour,\nci-joint nos factures."
    files, skipped = attachments(mail)
    assert [name for name, _, _ in files] == ["facture.pdf"]
    assert skipped == ["archive.zip : archive illisible"]  # a zip that is none: said, never silently dropped
    assert not automatic(mail)


@pytest.mark.functionality
def test_an_html_email_loses_its_tags_and_history():
    mail = EmailMessage()
    mail["From"] = "client@example.com"
    mail.set_content("<p>Oui, je confirme.</p><div>-----Original Message-----</div><p>ancien</p>", subtype="html")
    assert client_text(mail) == "Oui, je confirme."


@pytest.mark.functionality
@pytest.mark.parametrize(
    "header, value", [("Auto-Submitted", "auto-replied"), ("Precedence", "bulk"), ("From", "no-reply@example.com")]
)
def test_automatic_emails_are_never_answered(header, value):
    away = EmailMessage()
    away["From"] = "client@example.com"
    del away[header]
    away[header] = value
    assert automatic(away)
