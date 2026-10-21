"""The Discussion page's messages: what is refused before anything is saved or run, and how a saved message reads."""

import asyncio
import base64
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from starlette.requests import Request

from engine.chat import REMINDER
from tarik.core.conversation import FILES_NOTE
from ui import chat_pages, pages

SESSION = "chat-" + "0" * 32


def sent(payload, cabinet="cabinet_a", session=SESSION):
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body}

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-length", str(len(body)).encode())],
        "path_params": {"cabinet": cabinet, "session_id": session},
    }
    answer = asyncio.run(chat_pages.send(Request(scope, receive)))
    return answer.status_code, json.loads(answer.body).get("error", "")


def pdf(name="facture.pdf", data=b"%PDF-1.4"):
    return {"file_name": name, "mime_type": "application/pdf", "data": base64.b64encode(data).decode()}


@pytest.fixture(autouse=True)
def one_cabinet(monkeypatch):
    monkeypatch.setattr(chat_pages, "CABINETS", frozenset({"cabinet_a"}))


@pytest.mark.parametrize(
    ("payload", "cabinet", "session", "why"),
    [
        ({"message": "Bonjour"}, "cabinet_b", SESSION, "inconnue"),
        ({"message": "Bonjour"}, "cabinet_a", "mail-5fb65a1333466ddbe437", "inconnue"),  # never an email thread
        ({"message": " "}, "cabinet_a", SESSION, "joignez"),
        ({"message": "x" * 4001}, "cabinet_a", SESSION, "Message de 4001 caractères : 4000 au plus"),
        ({"files": [pdf("script.sh")]}, "cabinet_a", SESSION, "type non accepté"),
        ({"files": [pdf("../facture.pdf")]}, "cabinet_a", SESSION, "type non accepté"),
        ({"files": [pdf(data=b"")]}, "cabinet_a", SESSION, "vide"),
        ({"files": [pdf()] * 11}, "cabinet_a", SESSION, "11 fichiers joints : 10 au plus par message"),
        ({"files": [{"file_name": "f.pdf", "data": "pas du base64!"}]}, "cabinet_a", SESSION, "illisible"),
    ],
)
def test_a_wrong_message_is_refused_before_anything_is_done(payload, cabinet, session, why, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("nothing is saved or run for a refused message")

    monkeypatch.setattr(chat_pages, "call", never)
    monkeypatch.setattr(chat_pages.runs, "start", never)
    status, error = sent(payload, cabinet, session)
    assert status == 400 and why in error


@pytest.mark.communication
def test_a_refusal_names_every_cap_exceeded_and_how_to_stay_under_it(monkeypatch):
    monkeypatch.setattr(chat_pages, "MAX_FILE", chat_pages.MB)
    status, error = sent({"files": [pdf("gros.pdf", b"x" * 1_600_000), pdf("notes.docx")]}, "cabinet_a", SESSION)
    assert status == 400 and error.startswith("Rien n'a été envoyé :")
    assert "- gros.pdf : 1,5 Mo, 1 Mo au plus par fichier. Envoyez-le compressé" in error
    assert "- notes.docx : type non accepté. Types acceptés : PDF, JPG" in error


def test_the_limits_listed_are_the_ones_checked():
    shown = dict(chat_pages.LIMITS)
    assert f"{chat_pages.MAX_FILES} fichiers au plus" in shown["Par message"]
    assert f"{chat_pages.MAX_FILE // chat_pages.MB} Mo au plus" in shown["Par fichier"]
    assert "ZIP" in shown["Types acceptés"] and "100 fichiers et 200 Mo" in shown["Archive ZIP"]


def test_a_message_reads_as_written_with_its_files_and_reminders_left_out():
    assert chat_pages.said(HumanMessage(f"Voici{FILES_NOTE}a.pdf, b.png")) == {
        "who": "Client",
        "text": "Voici",
        "files": ["a.pdf", "b.png"],
        "skipped": [],
    }
    assert chat_pages.said(HumanMessage(f"Voici{FILES_NOTE}a.pdf{REMINDER}")) is None


def test_tarik_s_turn_shows_its_tools_as_they_run_then_its_reply(monkeypatch):
    class Memory:
        async def aget(self, config):
            return {"channel_values": {"messages": saved}}

    calls = [{"id": "1", "name": "Experio_find_dossier", "args": {}}, {"id": "2", "name": "Experio_list", "args": {}}]
    saved = [HumanMessage("Bonjour"), AIMessage("", tool_calls=calls), ToolMessage("ok", tool_call_id="1")]
    monkeypatch.setattr(chat_pages.agent, "CHECKPOINTER", Memory())
    turn = asyncio.run(chat_pages.messages("s"))[-1]
    assert [tool["state"] for tool in turn["tools"]] == ["done", "running"] and not turn["text"]
    saved += [ToolMessage("non", tool_call_id="2", status="error"), AIMessage("**Fait.**")]
    turn = asyncio.run(chat_pages.messages("s"))[-1]
    assert [tool["state"] for tool in turn["tools"]] == ["done", "failed"] and turn["text"] == "**Fait.**"


def test_a_reply_s_markdown_is_shown_without_its_html():
    shown = pages.markdown("**Fait** <img src=x onerror=alert(1)> [x](javascript:alert(1))")
    assert "<strong>Fait</strong>" in shown and "<img" not in shown and 'href="javascript' not in shown


def test_a_conversation_reads_as_where_it_stands():
    assert chat_pages.state("running", False) == "answering" and chat_pages.state("queued", True) == "answering"
    assert chat_pages.state("error", True) == "failed"
    assert chat_pages.state("done", True) == "waiting" and chat_pages.state("done", False) == "answered"


@pytest.mark.parametrize(
    ("metrics", "shown"),
    [
        ({"runs": 12, "seconds": 9.84, "failed": 0.125, "conversations": 3}, ["12", "9,8 s", "12,5 %", "3"]),
        ({"runs": 40, "seconds": 125.0, "failed": 0.0, "conversations": 9}, ["40", "2 min 5 s", "0 %", "9"]),
        ({"runs": 0, "seconds": None, "failed": None, "conversations": 0}, ["0", "—", "—", "0"]),
    ],
)
def test_the_dashboard_shows_its_numbers_as_the_team_reads_them(metrics, shown, monkeypatch):
    import re

    from ui import pages

    monkeypatch.setattr(pages, "CABINETS", {"cab_a"})
    monkeypatch.setattr(pages, "display_name", lambda cabinet: "Cabinet A")  # no Experio asked
    monkeypatch.setitem(pages.templates.env.globals, "display_name", pages.display_name)
    user = {"email": "a@experio.ma", "name": "A", "role": "member"}
    request = type(
        "R",
        (),
        {"cookies": {}, "url": type("U", (), {"path": "/ui/accueil"})(), "state": type("S", (), {"user": user})()},
    )
    page = pages.templates.get_template("dashboard.html").render(
        request=request(),
        cabinet="cab_a",
        metrics=metrics,
        days=30,
        new="chat-" + "0" * 32,
        messages=[],
        latest=None,
        going=False,
        **chat_pages.FORM,
    )
    assert re.findall(r'<span class="value num">([^<]*)</span>', page) == shown
    assert 'data-send="/ui/discussion/cab_a/chat-' in page and 'data-opens="yes"' in page


def test_the_dashboard_s_numbers_are_read_once_per_change_for_every_viewer(monkeypatch):
    from engine import live

    asked = []

    class Pool:
        def connection(self):
            class Db:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

                async def execute(self, statement, values):
                    asked.append(values)

                    class Rows:
                        async def fetchone(self):
                            return (len(asked), 1.0, 0.0, 1)

                    return Rows()

            return Db()

    monkeypatch.setattr(chat_pages.runs, "pool", Pool())
    monkeypatch.setattr(chat_pages, "NUMBERS", {})

    async def check():
        seen = [await chat_pages.metrics_of("cab_m") for _ in range(5)]  # five dashboards open on the cabinet
        assert len(asked) == 1 and all(numbers["runs"] == 1 for numbers in seen)
        live.touched("run-m", "cab_m", "chat-m")  # a run changes: the next look reads again, once
        assert (await chat_pages.metrics_of("cab_m"))["runs"] == 2
        assert (await chat_pages.metrics_of("cab_m"))["runs"] == 2 and len(asked) == 2

    asyncio.run(check())


def test_what_an_archive_left_out_shows_under_the_message():
    from tarik.core.conversation import handed

    text = handed("Voici", ["lot.zip › f.pdf"], ["lot.zip › notes.docx : type non pris en charge"])
    shown = chat_pages.said(HumanMessage(text))
    assert shown["text"] == "Voici" and shown["files"] == ["lot.zip › f.pdf"]
    assert shown["skipped"] == ["lot.zip › notes.docx : type non pris en charge"]
