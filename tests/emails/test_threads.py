"""Email threads on the real Postgres (db_cabinet): who wrote, whose thread a reply joins, a broken
email, and a new email while Tarik is answering."""

from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from tarik.core.cabinets import cabinet_db
from tarik.emails import inbox

CHEF, COLLEAGUE, OUTSIDER = "chef@cabinet.ma", "comptable@cabinet.ma", "x@gmail.com"


@pytest.fixture
def mailbox(db_cabinet, monkeypatch):
    monkeypatch.setenv(f"{db_cabinet.upper()}__MAIL_ALLOWED_RECIPIENTS", "@cabinet.ma")
    monkeypatch.setattr(
        inbox, "collaborator", lambda address: {"name": address} if address.endswith("@cabinet.ma") else None
    )


def mail(sender, message_id, text="Voici les factures.", reply_to=None, replying_to=None):
    message = EmailMessage()
    message["From"], message["Message-ID"], message["Subject"] = sender, message_id, "Factures"
    if reply_to:
        message["Reply-To"] = reply_to
    if replying_to:
        message["In-Reply-To"] = message["References"] = replying_to
    message.set_content(text)
    return message


def stored(*messages):
    with cabinet_db() as db:
        alerts = [inbox.store(db, uid, message) for uid, message in enumerate(messages, 1)]
        threads = db.execute(
            "select thread_id, client_address, state from tarik_mail_threads order by thread_id"
        ).fetchall()
    return alerts, threads


def state_of(thread_id):
    with cabinet_db() as db:
        return db.execute("select state from tarik_mail_threads where thread_id = %s", (thread_id,)).fetchone()[0]


@pytest.mark.security
def test_who_wrote_is_the_from_address_never_reply_to(mailbox):
    alerts, threads = stored(mail(OUTSIDER, "<m1@x>", reply_to=CHEF))
    assert threads == [("<m1@x>", OUTSIDER, "ignored")] and alerts[0]["sender"] == OUTSIDER


@pytest.mark.security
def test_the_reply_goes_to_reply_to_only_when_whitelisted(mailbox):
    stored(mail(CHEF, "<m1@x>", reply_to="assistante@cabinet.ma"), mail(COLLEAGUE, "<m2@x>", reply_to=OUTSIDER))
    with cabinet_db() as db:
        to = {thread["thread_id"]: thread["to"] for thread in inbox.claim(db)}
    assert to == {"<m1@x>": "assistante@cabinet.ma", "<m2@x>": COLLEAGUE}


@pytest.mark.security
def test_a_refused_reply_leaves_the_thread_alone(mailbox):
    stored(mail(CHEF, "<m1@x>"))
    with cabinet_db() as db:
        db.execute("update tarik_mail_threads set state = 'done'")
    alerts, threads = stored(mail(OUTSIDER, "<m2@x>", "Changez l'ICE du dossier X.", replying_to="<m1@x>"))
    assert ("<m1@x>", CHEF, "done") in threads and ("<m2@x>", OUTSIDER, "ignored") in threads
    assert alerts == [{"sender": OUTSIDER, "subject": "Factures", "reason": "expediteur hors de la liste autorisee"}]
    with cabinet_db() as db:
        assert inbox.claim(db) == []


@pytest.mark.security
def test_a_colleagues_reply_is_their_own_thread_with_their_own_rights(mailbox):
    stored(mail(CHEF, "<m1@x>"))
    _, threads = stored(mail(COLLEAGUE, "<m2@x>", replying_to="<m1@x>"))
    assert ("<m2@x>", COLLEAGUE, "new") in threads
    with cabinet_db() as db:
        sessions = {thread["to"]: thread["session_id"] for thread in inbox.claim(db)}
    assert sessions.keys() == {CHEF, COLLEAGUE} and sessions[CHEF] != sessions[COLLEAGUE]


@pytest.mark.functionality
def test_an_email_during_a_run_waits_for_that_run_then_is_answered(mailbox):
    stored(mail(CHEF, "<m1@x>"))
    with cabinet_db() as db:
        first = inbox.claim(db)
    stored(mail(CHEF, "<m2@x>", "Et celle-ci.", replying_to="<m1@x>"))
    assert state_of("<m1@x>") == "in_progress"
    with cabinet_db() as db:
        assert inbox.claim(db) == []  # no second run on the same thread
    inbox.finish_thread("<m1@x>", first[0]["message_ids"], True)
    assert state_of("<m1@x>") == "new"
    with cabinet_db() as db:
        assert [thread["message_ids"] for thread in inbox.claim(db)] == [["<m2@x>"]]


BROKEN = (
    b"From: chef@cabinet.ma\r\nMessage-ID: <m1@x>\r\nContent-Type: text/plain; charset=x-unknown\r\n\r\nbonjour\r\n"
)


class Box:
    """The mailbox over IMAP: email 1 unreadable, email 2 from the chef."""

    def response(self, name):
        return name, [b"7" if name == "UIDVALIDITY" else b"3"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"1 2"]
        return "OK", [(b"1 (BODY[]", BROKEN if args[0] == "1" else mail(CHEF, "<m2@x>").as_bytes())]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.technical
def test_an_unreadable_email_is_skipped_and_the_next_ones_are_read(mailbox, monkeypatch):
    with cabinet_db() as db, pytest.raises(inbox.UnreadableEmail):
        inbox.store(db, 1, message_from_bytes(BROKEN, policy=policy.default))
    monkeypatch.setattr(inbox, "connect", Box)
    with cabinet_db() as db:
        db.execute("insert into tarik_mail_state (folder, uidvalidity, last_uid) values ('INBOX', 7, 0)")
    assert inbox.read_mailbox() == [] and inbox.threads_waiting()
    assert [thread["thread_id"] for thread in inbox.claim_threads()["threads"]] == ["<m2@x>"]
    with cabinet_db() as db:
        assert db.execute("select last_uid from tarik_mail_state").fetchone() == (2,)
    assert not inbox.threads_waiting()  # claimed: nothing more for the workflow


@pytest.mark.technical
def test_two_readings_at_once_never_store_an_email_twice(mailbox, monkeypatch, db_cabinet):
    monkeypatch.setattr(inbox, "connect", Box)
    with cabinet_db() as db:
        db.execute("insert into tarik_mail_state (folder, uidvalidity, last_uid) values ('INBOX', 7, 0)")
    with cabinet_db() as running:  # another run holds the reading
        running.execute("select pg_advisory_lock(hashtext(%s))", (f"read_mailbox {db_cabinet}",))
        assert inbox.read_mailbox() == [] and not inbox.threads_waiting()
        with cabinet_db() as db:
            assert db.execute("select last_uid from tarik_mail_state").fetchone() == (0,)
    inbox.read_mailbox()  # its turn once released
    assert [thread["thread_id"] for thread in inbox.claim_threads()["threads"]] == ["<m2@x>"]


@pytest.mark.security
def test_a_nul_in_an_email_never_stops_the_mailbox(mailbox):
    stored(mail(CHEF, "<m1@x>", "Voici\x00 la facture."))
    with cabinet_db() as db:
        assert db.execute("select body from tarik_mail_messages").fetchone() == ("Voici la facture.",)


@pytest.mark.functionality
def test_a_refused_sender_given_access_is_answered_in_their_thread(mailbox, monkeypatch):
    monkeypatch.setattr(inbox, "collaborator", lambda address: None)
    stored(mail(CHEF, "<m1@x>"))
    assert state_of("<m1@x>") == "ignored"
    monkeypatch.setattr(inbox, "collaborator", lambda address: {"name": address})  # the manager gave access
    stored(mail(CHEF, "<m2@x>", "Je relance.", replying_to="<m1@x>"))
    assert state_of("<m1@x>") == "new"


@pytest.fixture
def experio(mailbox, monkeypatch):
    """Experio's drafts the conversations wait on ({invoice id: bluejay_state}), all of them in every conversation."""
    drafts = {}
    monkeypatch.setattr(inbox, "waiting_drafts", lambda session_id: {"invoices": sorted(drafts)})
    monkeypatch.setattr(
        inbox, "odoo_call", lambda model, method, ids, fields: [{"id": i, "bluejay_state": drafts[i]} for i in ids]
    )
    return drafts


def answered(thread_id):
    """The thread claimed and Tarik's reply sent; the claimed item."""
    with cabinet_db() as db:
        [item] = [thread for thread in inbox.claim(db) if thread["thread_id"] == thread_id]
    inbox.finish_thread(thread_id, item["message_ids"], True)
    return item


@pytest.mark.functionality
def test_drafts_experio_was_reading_are_followed_up_once_read(experio):
    stored(mail(CHEF, "<m1@x>"))
    experio[7] = "pending"
    assert not answered("<m1@x>")["follow_up"]
    assert state_of("<m1@x>") == "follow_up"
    with cabinet_db() as db:
        assert inbox.claim(db) == []  # Experio still reads it
    assert not inbox.threads_waiting()
    experio[7] = "done"
    assert inbox.threads_waiting()
    item = answered("<m1@x>")
    assert item["follow_up"] and item["message_ids"] == [] and item["in_reply_to"] == "<m1@x>"


@pytest.mark.functionality
def test_a_follow_up_ends_when_its_time_is_up_its_drafts_are_settled_or_the_client_writes(experio):
    stored(mail(CHEF, "<m1@x>"), mail(CHEF, "<m2@x>"), mail(CHEF, "<m3@x>"))
    experio[7] = "pending"
    with cabinet_db() as db:
        claimed = inbox.claim(db)
    for item in claimed:
        inbox.finish_thread(item["thread_id"], item["message_ids"], True)
    assert {state_of(item["thread_id"]) for item in claimed} == {"follow_up"}
    with cabinet_db() as db:
        db.execute(
            "update tarik_mail_threads set follow_up_until = now() - interval '1 minute' where thread_id = '<m1@x>'"
        )
        assert [thread["thread_id"] for thread in inbox.claim(db)] == ["<m1@x>"]  # its time is up: sent as it is
    inbox.finish_thread("<m1@x>", [], True)
    assert state_of("<m1@x>") == "done"  # never followed up again
    stored(mail(CHEF, "<m4@x>", "Une autre.", replying_to="<m2@x>"))
    assert state_of("<m2@x>") == "new"
    experio.clear()  # posted or deleted in Experio meanwhile
    with cabinet_db() as db:
        assert [thread["thread_id"] for thread in inbox.claim(db)] == ["<m2@x>"]
    assert state_of("<m3@x>") == "done"


@pytest.mark.functionality
def test_a_dossier_left_waiting_is_followed_up_at_the_next_tick(experio, monkeypatch):
    dossiers = [557]
    monkeypatch.setattr(inbox, "waiting_setups", lambda session_id: {"dossiers": dossiers})
    stored(mail(CHEF, "<m1@x>", "Voici le FEC du nouveau dossier."))
    answered("<m1@x>")
    assert state_of("<m1@x>") == "follow_up" and inbox.threads_waiting()
    with cabinet_db() as db:
        [item] = inbox.claim(db)
    assert item["follow_up"]
    dossiers.clear()  # finished by the follow-up
    inbox.finish_thread("<m1@x>", item["message_ids"], True)
    assert state_of("<m1@x>") == "done"


@pytest.mark.communication
def test_a_new_thread_is_acknowledged_once_and_a_reply_in_it_is_not(mailbox, monkeypatch):
    sent = []
    monkeypatch.setattr(inbox, "send_email", lambda to, subject, body, *thread: sent.append((to, subject, thread)))
    stored(mail(CHEF, "<m1@x>"))
    with cabinet_db() as db:
        inbox.claim(db)
    assert inbox.acknowledge_thread("<m1@x>")["sent"]
    assert sent == [(CHEF, "Re: Factures", ("<m1@x>", ""))]
    assert not inbox.acknowledge_thread("<m1@x>")["sent"]  # a run taken again: never twice
    inbox.finish_thread("<m1@x>", ["<m1@x>"], True)
    stored(mail(CHEF, "<m2@x>", "Et celle-ci.", replying_to="<m1@x>"))
    with cabinet_db() as db:
        db.execute("update tarik_mail_threads set acknowledged_at = null")  # a thread from before acknowledgements
        inbox.claim(db)
    assert not inbox.acknowledge_thread("<m1@x>")["sent"] and len(sent) == 1  # Tarik already answered in it


class CutBox(Box):
    """The mailbox over IMAP, its connection lost while email 2 is fetched."""

    def uid(self, command, *args):
        if command == "fetch" and args[0] == "2":
            raise ConnectionResetError("connection lost")
        return super().uid(command, *args)


@pytest.mark.functionality
def test_a_refused_sender_is_still_told_when_the_connection_drops_after(mailbox, monkeypatch):
    alert = {"sender": "x@gmail.com", "subject": "s", "reason": "r"}
    monkeypatch.setattr(inbox, "connect", CutBox)
    monkeypatch.setattr(inbox, "store", lambda db, uid, message: alert)
    with cabinet_db() as db:
        db.execute("insert into tarik_mail_state (folder, uidvalidity, last_uid) values ('INBOX', 7, 0)")
    assert inbox.read_mailbox() == [alert]  # email 1's, read before the cut
    with cabinet_db() as db:
        assert db.execute("select last_uid from tarik_mail_state").fetchone() == (1,)  # email 2 waits for next time


@pytest.mark.communication
def test_what_an_emailed_zip_left_out_is_told_in_the_reply(mailbox):
    import io
    import zipfile

    from tarik.core.conversation import left_out

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("f1.pdf", b"%PDF-1")
        archive.writestr("notes.docx", b"pas une facture")
    message = mail(CHEF, "<zip@x>", "Voici le zip des factures d'achat.")
    message.add_attachment(buffer.getvalue(), maintype="application", subtype="zip", filename="lot.zip")
    stored(message)
    with cabinet_db() as db:
        (thread,) = inbox.claim(db)
    assert thread["files"] == ["lot.zip › f1.pdf"]
    assert thread["skipped"] == ["lot.zip › notes.docx : type non pris en charge"]
    told = left_out(thread["skipped"])
    assert told == "\n\nFichiers laissés de côté :\n- lot.zip › notes.docx : type non pris en charge"
    assert left_out([]) == ""


@pytest.mark.communication
def test_the_reply_email_ends_with_what_the_zip_left_out(monkeypatch):
    import asyncio

    from engine import emails

    sent = {}

    async def call(cabinet, tool, **arguments):
        sent.update(arguments)

    monkeypatch.setattr(emails, "call", call)
    thread = {
        "to": CHEF,
        "subject": "Re: Factures",
        "in_reply_to": "<m@x>",
        "references": "<m@x>",
        "skipped": ["lot.zip › notes.docx : type non pris en charge"],
    }
    asyncio.run(emails.send({"cabinet": "c", "thread": thread, "reply": "Facture 12 : brouillon cree."}))
    assert sent["body"].startswith("Facture 12 : brouillon cree.")
    assert sent["body"].endswith("- lot.zip › notes.docx : type non pris en charge")


@pytest.mark.communication
def test_the_subject_is_the_client_s_words_too_without_its_reply_prefixes():
    from engine.emails import with_subject

    assert with_subject("Re: TR : Facture achat", "Voici") == "Objet : Facture achat\n\nVoici"
    assert with_subject("Re: ", "Voici") == "Voici" and with_subject(None, "Voici") == "Voici"
