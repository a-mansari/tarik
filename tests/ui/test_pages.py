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


def picked(cookie, path="/ui/executions"):
    return type(
        "R", (), {"cookies": {"tarik_cabinet": cookie} if cookie else {}, "url": type("U", (), {"path": path})}
    )()


def test_the_pages_show_the_cabinet_picked_else_the_first(monkeypatch):
    from ui import pages

    monkeypatch.setattr(pages, "CABINETS", {"cab_b", "cab_a"})
    monkeypatch.setattr(pages, "display_name", lambda cabinet: {"cab_a": "Cabinet A"}.get(cabinet, ""))
    assert pages.chosen(picked("cab_b")) == "cab_b"
    assert pages.chosen(picked("cab_gone")) == "cab_a" and pages.chosen(picked(None)) == "cab_a"
    shown = pages.sidebar(picked("cab_b", "/ui/discussion/chat-1"))
    assert [(c["name"], c["display"], c["chosen"]) for c in shown] == [
        ("cab_a", "Cabinet A", False),
        ("cab_b", "", True),
    ]


def test_picking_a_cabinet_keeps_it_and_opens_its_dashboard(monkeypatch):
    import asyncio

    from starlette.requests import Request

    from ui import pages

    monkeypatch.setattr(pages, "CABINETS", {"cab_a"})

    def pick(cabinet, target):
        async def receive():
            return {"type": "http.request", "body": f"cabinet={cabinet}".encode()}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/ui/cabinet",
            "headers": [],
            "query_string": f"next={target}".encode(),
        }
        return asyncio.run(pages.choose(Request(scope, receive)))

    answer = pick("cab_a", "/ui/llm-calls")
    assert answer.headers["location"] == "/ui/accueil" and "tarik_cabinet=cab_a" in answer.headers["set-cookie"]
    assert pick("cab_a", "//evil.example").headers["location"] == "/ui/accueil"  # never another site
    assert "set-cookie" not in pick("cab_unknown", "/ui/executions").headers
