"""What a routine sends on the user's side of a conversation (its instruction, the line its report opens on) is shown
as the routine's on every page, never as a client's: the Discussion page, a run's conversation, the conversations'
list. Against Tarik's memory as the agent writes it and this run's own run store."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from engine import agent, runs
from server.api import chat
from server.api import runs as runs_page
from tests.api.test_team_accounts import against_the_test_store

SESSION = "routine-2-202610281143"


@pytest.mark.communication
def test_the_discussion_page_shows_a_routine_s_report_as_the_routine_s_then_the_client_s_reply(monkeypatch):
    monkeypatch.setattr(agent, "CHECKPOINTER", InMemorySaver())
    asyncio.run(agent.began("c", SESSION, "Rappel Manager", "Par quoi commencer ?"))
    config = {"configurable": {"cabinet": "c", "session_id": SESSION, "thread_id": SESSION}}
    memory = agent.make_agent(config, chat=agent.ChatAnthropic(model="unused", api_key="unused"))
    reply = {"messages": [HumanMessage("Commence par ALPHA."), AIMessage("Je commence par ALPHA.")]}
    asyncio.run(memory.aupdate_state(config, reply, as_node="model"))
    shown = asyncio.run(chat.messages(SESSION))
    assert [(entry["who"], entry["text"]) for entry in shown] == [
        ("Routine", "[Routine : Rappel Manager]"),
        ("Tarik", "Par quoi commencer ?"),  # his report, its untrusted tags off
        ("Client", "Commence par ALPHA."),
        ("Tarik", "Je commence par ALPHA."),
    ]


@pytest.mark.communication
def test_a_run_s_conversation_shows_what_a_routine_sent_as_the_routine_s():
    shown = runs_page.conversation(
        [
            {"type": "human", "content": "Demande ses priorités au manager.", "name": agent.ROUTINE_NAME},
            {"type": "ai", "content": "Comment avancer aujourd'hui ?"},
            {"type": "human", "content": "Commence par ALPHA."},
        ]
    )
    assert [entry["who"] for entry in shown] == ["Routine", "Tarik", "Client"]


@pytest.mark.communication
def test_the_conversations_list_marks_a_conversation_a_routine_began(monkeypatch):
    async def check():
        call = {"routine_id": 2, "routine_name": "Rappel Manager", "subject": "Rappel Manager (à 9 h)"}
        period = await runs.created("routine", {"call": call}, "c", SESSION)
        instruction = {"message": "Demande ses priorités au manager."}
        await runs.created("discussion", instruction, "c", SESSION, None, period, "consigne")
        await runs.created("discussion", {"message": "Bonjour"}, "c", "chat-" + "0" * 32)
        report = "routine-1-202610281809"  # a progress routine's report, the manager replying by email
        subject = "Routine n° 1 : envoyer l'avancement (en semaine (du lundi au vendredi) à 9 h)"
        before = {"routine_id": 1, "subject": subject}
        await runs.created("routine", {"call": before}, "c", report)  # a period from before routine_name was kept
        await runs.created("fil", {"thread": {"message": "Merci", "subject": "Re: Avancement"}}, "c", report)
        found = await runs_page.openings("c", [SESSION, "chat-" + "0" * 32, report])
        assert found[SESSION]["routine"] == {"id": 2, "name": "Rappel Manager"}
        assert found[SESSION]["first"] == "Demande ses priorités au manager."
        # its report began it, the reply came after; its name read back from its subject, the schedule dropped
        assert found[report]["routine"] == {"id": 1, "name": "Routine n° 1 : envoyer l'avancement"}
        assert found["chat-" + "0" * 32]["routine"] is None  # a client's own conversation

    monkeypatch.setattr(runs.live, "touched", lambda *args: None)
    against_the_test_store(check, monkeypatch)


@pytest.mark.communication
def test_a_routine_s_instruction_on_its_way_shows_as_the_routine_s_never_twice(monkeypatch):
    async def run(run_id):
        return {"input": {"message": "Demande ses priorités au manager."}}

    monkeypatch.setattr(chat.runs, "run", run)
    latest = {"run_id": "r", "session_id": SESSION}
    pending = asyncio.run(chat.on_its_way(latest, []))
    assert pending["who"] == "Routine" and pending["text"] == "Demande ses priorités au manager."
    kept = [{"who": "Routine", "text": "Demande ses priorités au manager."}]
    assert asyncio.run(chat.on_its_way(latest, kept)) is None  # in Tarik's memory already: shown once
    client = asyncio.run(chat.on_its_way({"run_id": "r", "session_id": "chat-" + "0" * 32}, []))
    assert client["who"] == "Client"


@pytest.mark.communication
@pytest.mark.parametrize(
    ("still_there", "shown"), [(True, ("Rappel du matin", False)), (False, ("Rappel Manager", True))]
)
def test_a_routine_s_conversation_is_listed_as_email_with_the_routine_that_began_it(monkeypatch, still_there, shown):
    latest = {"run_id": "r", "session_id": SESSION, "graph": "discussion", "status": "running", "started": "t"}

    async def recent(cabinet, graph, limit):
        return [latest] if graph == "discussion" else []  # only the routine's own Discussion run so far

    async def nothing_waits(cabinet, sessions):
        return set()

    async def opened(cabinet, sessions):
        began = {"id": 2, "name": "Rappel Manager"}  # what it was called then
        return {SESSION: {"first": "Demande ses priorités.", "subject": None, "sender": None, "routine": began}}

    async def within(cabinet, function, *args):  # the cabinet's routines now: renamed since, or deleted
        return [{"id": 2, "kind": "instruction", "name": "Rappel du matin"}] if still_there else []

    monkeypatch.setattr(chat.runs, "recent", recent)
    monkeypatch.setattr(chat, "waiting_for_client", nothing_waits)
    monkeypatch.setattr(chat, "openings", opened)
    monkeypatch.setattr(chat, "within", within)
    (listed,) = asyncio.run(chat.conversations("c"))
    assert listed["medium"] == "email"  # its channel; what began it after
    assert (listed["routine"]["name"], listed["routine"]["deleted"]) == shown
