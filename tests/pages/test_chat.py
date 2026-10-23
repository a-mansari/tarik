"""The Discussion page's messages: what is refused before anything is saved or run, and how a saved message reads."""

import asyncio
import base64
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from starlette.requests import Request

from app import pages
from engine.chat import REMINDER
from tarik.core.conversation import FILES_NOTE

SESSION = "chat-" + "0" * 32


def sent(payload, cabinet="cabinet_a", session=SESSION, shown=None):
    """``payload`` posted by the page of ``shown`` (default: ``cabinet``), ``cabinet`` picked in the sidebar."""
    body = json.dumps({"cabinet": shown or cabinet, **payload}).encode()

    async def receive():
        return {"type": "http.request", "body": body}

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-length", str(len(body)).encode()), (b"cookie", f"tarik_cabinet={cabinet}".encode())],
        "path_params": {"session_id": session},
    }
    answer = asyncio.run(pages.chat.send(Request(scope, receive)))
    return answer.status_code, json.loads(answer.body).get("error", "")


def pdf(name="facture.pdf", data=b"%PDF-1.4"):
    return {"file_name": name, "mime_type": "application/pdf", "data": base64.b64encode(data).decode()}


@pytest.fixture(autouse=True)
def one_cabinet(monkeypatch):
    monkeypatch.setattr(pages.chat, "CABINETS", frozenset({"cabinet_a"}))
    monkeypatch.setattr(pages.runs, "CABINETS", frozenset({"cabinet_a"}))


def test_a_message_from_a_page_of_another_cabinet_than_the_one_picked_is_refused(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("nothing is saved or run in the cabinet picked since")

    monkeypatch.setattr(pages.chat, "call", never)
    monkeypatch.setattr(pages.chat.runs, "start", never)
    status, error = sent({"message": "Bonjour"}, "cabinet_a", SESSION, shown="cabinet_b")
    assert status == 400 and "autre cabinet" in error


@pytest.mark.parametrize(
    ("payload", "cabinet", "session", "why"),
    [
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

    monkeypatch.setattr(pages.chat, "call", never)
    monkeypatch.setattr(pages.chat.runs, "start", never)
    status, error = sent(payload, cabinet, session)
    assert status == 400 and why in error


@pytest.mark.communication
def test_a_refusal_names_every_cap_exceeded_and_how_to_stay_under_it(monkeypatch):
    monkeypatch.setattr(pages.chat, "MAX_FILE", pages.chat.MB)
    status, error = sent({"files": [pdf("gros.pdf", b"x" * 1_600_000), pdf("notes.docx")]}, "cabinet_a", SESSION)
    assert status == 400 and error.startswith("Rien n'a été envoyé :")
    assert "- gros.pdf : 1,5 Mo, 1 Mo au plus par fichier. Envoyez-le compressé" in error
    assert "- notes.docx : type non accepté. Types acceptés : PDF, JPG" in error


def test_the_limits_listed_are_the_ones_checked():
    shown = dict(pages.chat.LIMITS)
    assert f"{pages.chat.MAX_FILES} fichiers au plus" in shown["Par message"]
    assert f"{pages.chat.MAX_FILE // pages.chat.MB} Mo au plus" in shown["Par fichier"]
    assert "ZIP" in shown["Types acceptés"] and "100 fichiers et 200 Mo" in shown["Archive ZIP"]


def test_a_message_reads_as_written_with_its_files_and_reminders_left_out():
    assert pages.chat.said(HumanMessage(f"Voici{FILES_NOTE}a.pdf, b.png")) == {
        "who": "Client",
        "text": "Voici",
        "files": ["a.pdf", "b.png"],
        "skipped": [],
    }
    assert pages.chat.said(HumanMessage(f"Voici{FILES_NOTE}a.pdf{REMINDER}")) is None


def test_tarik_s_turn_shows_its_tools_as_they_run_then_its_reply(monkeypatch):
    class Memory:
        async def aget(self, config):
            return {"channel_values": {"messages": saved}}

    calls = [{"id": "1", "name": "Experio_find_dossier", "args": {}}, {"id": "2", "name": "Experio_list", "args": {}}]
    saved = [HumanMessage("Bonjour"), AIMessage("", tool_calls=calls), ToolMessage("ok", tool_call_id="1")]
    monkeypatch.setattr(pages.chat.agent, "CHECKPOINTER", Memory())
    turn = asyncio.run(pages.chat.messages("s"))[-1]
    assert [tool["state"] for tool in turn["tools"]] == ["done", "running"] and not turn["text"]
    saved += [ToolMessage("non", tool_call_id="2", status="error"), AIMessage("**Fait.**")]
    turn = asyncio.run(pages.chat.messages("s"))[-1]
    assert [tool["state"] for tool in turn["tools"]] == ["done", "failed"] and turn["text"] == "**Fait.**"


def test_a_reply_s_markdown_is_shown_without_its_html():
    shown = pages.runs.markdown("**Fait** <img src=x onerror=alert(1)> [x](javascript:alert(1))")
    assert "<strong>Fait</strong>" in shown and "<img" not in shown and 'href="javascript' not in shown


def test_a_conversation_reads_as_where_it_stands():
    assert pages.chat.state("running", False) == "answering" and pages.chat.state("queued", True) == "answering"
    assert pages.chat.state("error", True) == "failed"
    assert pages.chat.state("done", True) == "waiting" and pages.chat.state("done", False) == "answered"


@pytest.mark.parametrize(
    ("metrics", "shown"),
    [
        ({"runs": 12, "seconds": 9.84, "failed": 0.125, "conversations": 3}, ["12", "9,8 s", "12,5 %", "3"]),
        ({"runs": 40, "seconds": 125.0, "failed": 0.0, "conversations": 9}, ["40", "2 min 5 s", "0 %", "9"]),
        ({"runs": 0, "seconds": None, "failed": None, "conversations": 0}, ["0", "—", "—", "0"]),
    ],
)
def test_the_dashboard_shows_its_numbers_as_the_team_reads_them(metrics, shown, monkeypatch):
    readable = pages.chat.readable(metrics)
    assert [readable[key] for key in ("runs", "seconds", "failed", "conversations")] == shown
    assert readable["bad"] == bool(metrics["failed"])

    async def numbers(cabinet):
        return metrics

    monkeypatch.setattr(pages.chat, "metrics_of", numbers)
    scope = {"type": "http", "headers": [(b"cookie", b"tarik_cabinet=cabinet_a")]}
    page = json.loads(asyncio.run(pages.chat.dashboard_page(Request(scope))).body)
    assert page["cabinet"] == "cabinet_a" and page["metrics"] == readable
    assert pages.chat.SESSION.fullmatch(page["new"]) and page["checks"]["max_files"] == pages.chat.MAX_FILES


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

    monkeypatch.setattr(pages.chat.runs, "pool", Pool())
    monkeypatch.setattr(pages.chat, "NUMBERS", {})

    async def check():
        seen = [await pages.chat.metrics_of("cab_m") for _ in range(5)]  # five dashboards open on the cabinet
        assert len(asked) == 1 and all(numbers["runs"] == 1 for numbers in seen)
        live.touched("run-m", "cab_m", "chat-m")  # a run changes: the next look reads again, once
        assert (await pages.chat.metrics_of("cab_m"))["runs"] == 2
        assert (await pages.chat.metrics_of("cab_m"))["runs"] == 2 and len(asked) == 2

    asyncio.run(check())


def test_what_an_archive_left_out_shows_under_the_message():
    from tarik.core.conversation import handed

    text = handed("Voici", ["lot.zip › f.pdf"], ["lot.zip › notes.docx : type non pris en charge"])
    shown = pages.chat.said(HumanMessage(text))
    assert shown["text"] == "Voici" and shown["files"] == ["lot.zip › f.pdf"]
    assert shown["skipped"] == ["lot.zip › notes.docx : type non pris en charge"]


def test_a_client_message_reads_as_markdown_with_its_html_and_unsafe_links_left_inert():
    shown = str(
        pages.runs.markdown("**urgent**\n- facture 12\n\n<script>alert(1)</script> [lien](javascript:alert(1))")
    )
    assert "<strong>urgent</strong>" in shown and "<li>facture 12</li>" in shown
    assert "<script>" not in shown and 'href="javascript:' not in shown


def test_a_conversation_s_words_reach_the_page_only_as_the_server_s_markdown(monkeypatch):
    async def talk(cabinet, session):
        said = {"who": "Client", "text": "**urgent** <script>alert(1)</script>", "files": [], "skipped": []}
        return {"messages": [said, {"who": "Tarik", "tools": [], "text": ""}], "latest": None, "going": False}

    monkeypatch.setattr(pages.chat, "talk", talk)
    scope = {
        "type": "http",
        "headers": [(b"cookie", b"tarik_cabinet=cabinet_a")],
        "path_params": {"session_id": SESSION},
    }
    page = json.loads(asyncio.run(pages.chat.conversation_page(Request(scope))).body)
    client, tarik = page["messages"]
    assert "<strong>urgent</strong>" in client["text_html"] and "<script>" not in client["text_html"]
    assert tarik["text_html"] == "" and page["written"] is True
    scope["path_params"] = {"session_id": "../etc"}
    assert asyncio.run(pages.chat.conversation_page(Request(scope))).status_code == 404
