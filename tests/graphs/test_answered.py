"""Tarik's reply to a message (engine.chat.answered): asked again with a reminder while the files stay untaken, and
once when it called no tool while a question waits on the client."""

import asyncio
from types import SimpleNamespace

import pytest

from engine import chat, runs


def turn(*kinds, text="reponse"):
    messages = [SimpleNamespace(type="human", text="message")]
    messages += [SimpleNamespace(type=kind, text=text) for kind in kinds]
    return {"messages": messages}


@pytest.fixture
def asking(monkeypatch):
    state = {"asked": [], "turns": [], "untaken": [], "waiting": []}

    async def nested(name, given, cabinet, session, thread=None, node=None):
        state["asked"].append(given["messages"][0].content)
        return state["turns"].pop(0) if len(state["turns"]) > 1 else state["turns"][0]

    async def within(cabinet, function, *args):
        if function is chat.untaken:
            return state["untaken"].pop(0) if state["untaken"] else False
        if function is chat.waiting_on:
            return state["waiting"].pop(0) if state["waiting"] else []
        return None

    monkeypatch.setattr(runs, "nested", nested)
    monkeypatch.setattr(chat, "within", within)
    return state


def reply(state, files=()):
    return asyncio.run(chat.answered("c", "s", "sans verification", list(files), "tarik"))


@pytest.mark.functionality
def test_a_reply_calling_no_tool_while_a_question_waits_is_asked_once_again(asking):
    asking["turns"] = [turn("ai", text="L'outil n'est pas disponible"), turn("ai", "tool", "ai", text="fait")]
    asking["waiting"] = [[{"step": "link_tiers"}]]
    assert reply(asking) == "fait"
    assert len(asking["asked"]) == 2 and "Experio_repondre" in asking["asked"][1]


@pytest.mark.functionality
def test_a_reply_without_a_tool_and_no_question_waiting_is_given_as_is(asking):
    asking["turns"] = [turn("ai", text="Bonjour, de rien !")]
    assert reply(asking) == "Bonjour, de rien !" and len(asking["asked"]) == 1


@pytest.mark.functionality
def test_the_question_reminder_is_given_once_only(asking):
    asking["turns"] = [turn("ai", text="merci")]
    asking["waiting"] = [[{"step": "settle_tva"}], [{"step": "settle_tva"}]]
    reply(asking)
    assert len(asking["asked"]) == 2  # the client may only be thanking: never asked a third time


@pytest.mark.functionality
def test_untaken_files_are_reminded_until_taken(asking):
    asking["turns"] = [turn("ai"), turn("ai", "tool", "ai", text="traite")]
    asking["untaken"] = [True, False]
    assert reply(asking, files=["f.pdf"]) == "traite"
    assert "ces fichiers n'ont pas ete traites" in asking["asked"][1]
