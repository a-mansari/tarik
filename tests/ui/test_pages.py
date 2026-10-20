"""What a run was asked, as the run's page shows it: each input under its plain name, coded values in words, an email
thread as its sender, subject and message, and nothing internal."""

from ui.pages import asked


def test_a_workflow_s_inputs_read_in_plain_words():
    call = {
        "mode": "files",
        "dossier": "TARIK TEST",
        "client_confirmed": True,
        "skip_ice_if_check": False,
        "session_id": "s",
        "cabinet": "c",
        "attachments": [{"data": "..."}],
        "compte": "",
    }
    shown = asked({"graph": "traitement", "input": {"call": call}})
    assert shown == {
        "Ce qui est demandé": "Traiter les fichiers joints",
        "Dossier": "TARIK TEST",
        "Confirmé par le client": "Oui",
    }


def test_an_email_thread_reads_as_its_sender_subject_and_message():
    thread = {"to": "client@cabinet.ma", "subject": "Factures", "message": "Voici", "files": ["a.pdf", "b.pdf"]}
    shown = asked({"graph": "fil", "input": {"cabinet": "c", "thread": thread, "done": []}})
    expected = {"De": "client@cabinet.ma", "Sujet": "Factures", "Message": "Voici", "Fichiers joints": "a.pdf, b.pdf"}
    assert shown == expected
