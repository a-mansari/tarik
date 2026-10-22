"""Incoming emails: the client's own words, their files, automatic replies."""

from email.message import EmailMessage

import pytest

from tarik.emails.inbox import attachments, automatic, client_text, without_signature


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
    mail.add_attachment(b"doc", maintype="application", subtype="msword", filename="notes.docx")
    assert client_text(mail) == "Bonjour,\nci-joint nos factures."
    files, skipped = attachments(mail)
    assert [name for name, _, _ in files] == ["facture.pdf"]
    # A zip that is none, a type Tarik does not take: said, never silently dropped.
    assert skipped == ["notes.docx : type non pris en charge", "archive.zip : archive illisible"]
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


@pytest.mark.communication
def test_a_signature_s_images_are_no_words_of_the_client():
    mail = EmailMessage()
    mail.set_content(
        "Voila ma facture\n\n[Image]\nHICHAM MOUTI\n[EXPERIO png bleu.png]\n[Image]  +212 663-785382\n"
        "[cid:image001.png@01DB]   www.experio.ma\n[1] la note de bas de page reste"
    )
    text = client_text(mail)
    assert "[" not in text.replace("[1]", "") and "Voila ma facture" in text and "+212 663-785382" in text


@pytest.mark.functionality
@pytest.mark.parametrize(
    "body, said",
    [
        (  # below the quote
            "Le mar. 21 oct. 2026, Tarik a écrit :\n> Quel compte bancaire ?\n\nBMCE, merci.",
            "BMCE, merci.",
        ),
        (  # between its lines
            "Voici mes réponses.\n\nLe mar. 21 oct. 2026, Tarik a écrit :\n> 1. TVA récupérable ?\nNon.\n"
            "> 2. Quel exercice ?\nExercice 2025.",
            "Voici mes réponses.\n\nNon.\nExercice 2025.",
        ),
        (  # Outlook's line of underscores over the earlier email
            "achat\n\nAchraf Mansari\n________________________________\nDe : Tarik\nAncien texte.",
            "achat\n\nAchraf Mansari",
        ),
        (  # the earlier email unmarked after its header (Outlook): never the client's
            "Oui, comptabilisez.\n\nDe : Tarik\nEnvoyé : mardi\nObjet : Factures\n\nAncien texte, comptabilisez tout.",
            "Oui, comptabilisez.",
        ),
    ],
)
def test_what_the_client_writes_below_or_between_a_quote_is_theirs(body, said):
    mail = EmailMessage()
    mail["From"] = "client@example.com"
    mail.set_content(body)
    assert client_text(mail) == said


@pytest.mark.functionality
def test_an_html_list_keeps_one_line_per_item():
    mail = EmailMessage()
    mail["From"] = "client@example.com"
    mail.set_content("<ul><li>272459 : non</li><li>272471 : nouveau</li></ul>", subtype="html")
    assert client_text(mail) == "272459 : non\n272471 : nouveau"


SIGNATURE = "\n\n[Achraf Mansari]\nAchraf Mansari\nStagiaire @Experio\n+212 610312891\nwww.experio.ma"


@pytest.mark.functionality
def test_the_signature_the_sender_s_other_emails_end_with_is_cut():
    earlier = ["Merci d'ajouter cette facture au dossier tarik test" + SIGNATURE]
    assert without_signature("utilisez transport ou hotelrie" + SIGNATURE, earlier) == "utilisez transport ou hotelrie"
    assert without_signature("achat" + SIGNATURE, []) == "achat" + SIGNATURE  # their first email: nothing to compare
    assert without_signature("Merci" + SIGNATURE, ["Merci" + SIGNATURE]) == "Merci" + SIGNATURE  # the same email again
    assert without_signature("achat" + SIGNATURE, ["achat" + SIGNATURE, *earlier]) == "achat"


@pytest.mark.security
def test_the_client_s_own_words_are_never_taken_for_a_signature():
    assert without_signature("achat\nTARIK TEST", ["vente\nachat\nTARIK TEST"]) == "achat\nTARIK TEST"  # 2 lines
    assert without_signature(SIGNATURE.strip(), ["Bonjour" + SIGNATURE]) == SIGNATURE.strip()  # nothing would be left
