"""Email threads on the real Postgres (db_cabinet): who wrote, whose thread a reply joins, a broken
email, and a new email while Tarik is answering."""

from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from engine.tools.core.cabinets import cabinet_db
from engine.tools.emails import inbox

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
    reason = "L'expéditeur n'est pas dans la liste des adresses autorisées."
    assert alerts == [{"sender": OUTSIDER, "subject": "Factures", "reason": reason}]
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


@pytest.mark.functionality
def test_a_reply_naming_only_tarik_s_email_stays_in_its_thread(mailbox):
    stored(mail(CHEF, "<m1@x>"))
    with cabinet_db() as db:
        (item,) = inbox.claim(db)
    inbox.finish_thread("<m1@x>", item["message_ids"], True, sent_id="<t1@cabinet.ma>")
    _, threads = stored(mail(CHEF, "<m2@x>", "Par document.", replying_to="<t1@cabinet.ma>"))
    assert [thread for thread, _, _ in threads] == ["<m1@x>"]  # its memory, files and dossier with it


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
def test_a_dossier_left_waiting_is_followed_up_once_its_lines_are_processed(experio, monkeypatch):
    dossiers, settled = [557], []
    monkeypatch.setattr(inbox, "waiting_setups", lambda session_id: {"dossiers": dossiers})
    monkeypatch.setattr(inbox, "setups_settled", lambda ids: bool(settled))
    stored(mail(CHEF, "<m1@x>", "Voici le FEC du nouveau dossier."))
    answered("<m1@x>")
    assert state_of("<m1@x>") == "follow_up" and not inbox.threads_waiting()  # Experio still processing its lines
    with cabinet_db() as db:
        assert inbox.claim(db) == []
    settled.append(True)
    assert inbox.threads_waiting()
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

    from engine.tools.core.conversation import left_out

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
    assert told == "\n\n**Fichiers laissés de côté**\n- `lot.zip › notes.docx` : type non pris en charge"
    assert left_out([]) == ""


@pytest.mark.communication
def test_the_client_never_reads_the_agent_s_question_marks():
    from engine.tools.core.conversation import for_client

    reply = "**Questions**\n1. QUESTION : Facture 12 : la TVA est-elle non récupérable ? oui / non"
    assert for_client(reply) == "**Questions**\n1. Facture 12 : la TVA est-elle non récupérable ? oui / non"


@pytest.mark.communication
def test_an_answer_is_quoted_back_without_its_email_subject():
    from engine.tools.core.conversation import without_subject

    assert without_subject("Objet : Facture fournisseur\n\n6142 Voyages.") == "6142 Voyages."
    assert without_subject("6142 Voyages.") == "6142 Voyages."


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
    assert sent["body"].endswith("- `lot.zip › notes.docx` : type non pris en charge")


@pytest.mark.communication
def test_the_subject_is_the_client_s_words_too_without_its_reply_prefixes():
    from engine.emails import with_subject

    assert with_subject("Re: TR : Facture achat", "Voici") == "Objet : Facture achat\n\nVoici"
    assert with_subject("Re: ", "Voici") == "Voici" and with_subject(None, "Voici") == "Voici"


@pytest.mark.technical
def test_experio_out_keeps_a_follow_up_for_the_next_tick(experio, monkeypatch):
    stored(mail(CHEF, "<m1@x>"))
    experio[7] = "pending"
    answered("<m1@x>")

    def down(*args, **kw):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(inbox, "odoo_call", down)
    assert not inbox.threads_waiting() and state_of("<m1@x>") == "follow_up"


@pytest.mark.technical
def test_drafts_experio_cannot_list_are_none_to_resume(monkeypatch):
    def down(session_id):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(inbox, "waiting_drafts", down)
    assert inbox.drafts_to_resume("s") == []


def manager(monkeypatch, db_cabinet, responsable, login, fallback=""):
    calls = []

    def odoo_call(model, method, ids, fields):
        calls.append(model)
        if model == "res.company":
            return [{"responsable_id": responsable}]
        return [{"login": login}] if login else []

    monkeypatch.setattr(inbox, "odoo_call", odoo_call)
    monkeypatch.setattr(inbox, "main_company_id", lambda: 1)
    monkeypatch.setenv(f"{db_cabinet.upper()}__MAIL_MANAGER", fallback)
    return calls


@pytest.mark.security
@pytest.mark.parametrize(
    ("responsable", "login", "fallback", "found"),
    [
        ([4, "Chef"], CHEF, "", CHEF),  # the Responsable of the cabinet's company in Experio
        ([4, "Chef"], OUTSIDER, COLLEAGUE, COLLEAGUE),  # not a collaborator: MAIL_MANAGER
        ([4, "Chef"], None, COLLEAGUE, COLLEAGUE),  # its user gone
        (False, None, f" {COLLEAGUE} ", COLLEAGUE),
        (False, None, OUTSIDER, None),  # MAIL_MANAGER must be a collaborator too
        (False, None, "", None),
    ],
)
def test_the_manager_is_experio_responsable_else_mail_manager_a_collaborator_only(
    mailbox, db_cabinet, monkeypatch, responsable, login, fallback, found
):
    calls = manager(monkeypatch, db_cabinet, responsable, login, fallback)
    assert inbox.cabinet_manager() == found
    assert calls == (["res.company", "res.users"] if responsable else ["res.company"])


@pytest.mark.communication
def test_the_manager_hears_of_a_refused_sender_once_a_day_and_the_sender_never(mailbox, db_cabinet, monkeypatch):
    manager(monkeypatch, db_cabinet, [4, "Chef"], CHEF)
    sent = []
    monkeypatch.setattr(inbox, "send_email", lambda to, subject, body: sent.append((to, subject, body)))
    assert inbox.notify_manager(OUTSIDER, "", "L'expéditeur n'est pas autorisé.")["status"] == "ok"
    assert inbox.notify_manager(OUTSIDER, "Factures", "r")["message"].startswith("Manager déjà prévenu")
    assert [(to, subject) for to, subject, _ in sent] == [(CHEF, f"Tarik : email de {OUTSIDER} non traité")]
    assert "« sans objet »" in sent[0][2] and "aucune réponse" in sent[0][2]


@pytest.mark.communication
def test_without_a_manager_the_alert_fails_loudly(mailbox, db_cabinet, monkeypatch):
    manager(monkeypatch, db_cabinet, False, None)
    monkeypatch.setattr(inbox, "send_email", lambda *a: pytest.fail("nobody to tell"))
    with pytest.raises(inbox.ToolError, match="Manager non prévenu"):
        inbox.notify_manager(OUTSIDER, "s", "r")


@pytest.mark.functionality
def test_a_reply_to_a_routine_s_report_is_answered_in_the_routine_s_own_conversation(mailbox):
    inbox.opened_thread("<rapport@tarik>", "Chef@cabinet.ma", "Rappel Manager", "routine-2-202610290900")
    _, threads = stored(mail(CHEF, "<r1@x>", "Commence par les factures de ALPHA.", replying_to="<rapport@tarik>"))
    assert threads == [("<rapport@tarik>", CHEF, "new")]  # its thread, back to Tarik: no thread of its own
    with cabinet_db() as db:
        (item,) = inbox.claim(db)
    assert item["session_id"] == "routine-2-202610290900"  # the report, its files and what Tarik said are there
    assert item["message"] == "Commence par les factures de ALPHA." and item["in_reply_to"] == "<r1@x>"


@pytest.mark.security
def test_someone_else_replying_to_a_routine_s_report_gets_a_thread_of_their_own(mailbox):
    inbox.opened_thread("<rapport@tarik>", CHEF, "Rappel Manager", "routine-2-202610290900")
    _, threads = stored(mail(COLLEAGUE, "<r1@x>", "Fais tout.", replying_to="<rapport@tarik>"))
    assert ("<r1@x>", COLLEAGUE, "new") in threads and ("<rapport@tarik>", CHEF, "done") in threads
    with cabinet_db() as db:
        (item,) = inbox.claim(db)
    assert item["session_id"] != "routine-2-202610290900"  # never the routine's conversation: not sent to them
