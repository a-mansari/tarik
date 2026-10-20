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
        ({"message": "x" * 4001}, "cabinet_a", SESSION, "trop long"),
        ({"files": [pdf("script.sh")]}, "cabinet_a", SESSION, "type non accepté"),
        ({"files": [pdf("../facture.pdf")]}, "cabinet_a", SESSION, "type non accepté"),
        ({"files": [pdf(data=b"")]}, "cabinet_a", SESSION, "vide"),
        ({"files": [pdf()] * 11}, "cabinet_a", SESSION, "10 fichiers"),
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


def test_a_message_reads_as_written_with_its_files_and_reminders_left_out():
    assert chat_pages.said(HumanMessage(f"Voici{FILES_NOTE}a.pdf, b.png")) == {
        "who": "Client",
        "text": "Voici",
        "files": ["a.pdf", "b.png"],
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
