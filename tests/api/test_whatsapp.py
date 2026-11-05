"""Sparrow's forward of a WhatsApp message: only an enrolled person's is answered, once, one after the other."""

import asyncio
import base64
import json

import pytest
from starlette.requests import Request

from engine.tools.dossiers import people
from server import api
from server.api import whatsapp

RECIPIENT = "r" * 16
WHO = ("cabinet_a", "amal@cabinet.ma", "wa-session")


class World:
    """The server around the handler, faked: what the run answered, what was saved, started and sent."""

    def __init__(self):
        self.enrolled = {RECIPIENT: WHO}
        self.taken = set()
        self.collaborator = True
        self.reply = {"reply": "Bonjour Amal"}
        self.pause = 0
        self.events = []  # ("save", files) ("start", message, writer) ("end", message) ("send", recipient, text)
        self.remembered = []


@pytest.fixture
def world(monkeypatch):
    world = World()
    monkeypatch.setattr(whatsapp, "CABINETS", frozenset({"cabinet_a"}))

    async def recipient_of(recipient_id):
        return world.enrolled.get(recipient_id)

    async def first_time(message_id):
        fresh = message_id not in world.taken
        world.taken.add(message_id)
        return fresh

    async def within(cabinet, function, /, *args):
        assert cabinet == "cabinet_a"
        if function is people.collaborator:
            return world.collaborator
        if function is people.remember_whatsapp_sender:
            return world.remembered.append(args)
        if function is whatsapp.sparrow.send:
            return world.events.append(("send", *args))
        raise AssertionError(function)

    async def call(cabinet, tool, **arguments):
        world.events.append(("save", [f["file_name"] for f in arguments["files"]]))

    async def start(name, payload):
        assert name == "discussion" and payload["session_id"] == "wa-session"
        message = payload["input"]["message"]
        if isinstance(world.reply, Exception):
            raise world.reply
        world.events.append(("start", message, whatsapp.runs.WRITER.get()))

        async def finish():
            await asyncio.sleep(world.pause)
            world.events.append(("end", message))
            return world.reply, None

        return "run-1", asyncio.ensure_future(finish())

    monkeypatch.setattr(whatsapp, "recipient_of", recipient_of)
    monkeypatch.setattr(whatsapp, "first_time", first_time)
    monkeypatch.setattr(whatsapp, "within", within)
    monkeypatch.setattr(whatsapp, "call", call)
    monkeypatch.setattr(whatsapp.runs, "start", start)
    return world


def forward(*messages, bearer=RECIPIENT, raw=None):
    """Post each of ``messages`` (dicts, or ``raw`` bytes) as Sparrow does and return the statuses, once every answer
    started has ended."""

    async def main():
        statuses = []
        for message in messages:
            body = raw if raw is not None else json.dumps(message).encode()

            async def receive(body=body):
                return {"type": "http.request", "body": body}

            headers = [(b"authorization", f"Bearer {bearer}".encode())] if bearer is not None else []
            answer = await api.whatsapp.handler(Request({"type": "http", "headers": headers}, receive))
            statuses.append(answer.status_code)
        await asyncio.gather(*whatsapp.TASKS)
        return statuses

    return asyncio.run(main())


def text(message_id, content="Bonjour"):
    return {"message_id": message_id, "reply_to_message_id": None, "content": content}


@pytest.mark.security
@pytest.mark.parametrize("bearer", ["f" * 16, "", None])
def test_a_message_from_nobody_enrolled_is_refused_and_nothing_runs(world, bearer):
    assert forward(text("m1"), bearer=bearer) == [401]
    assert world.events == [] and world.taken == set()


@pytest.mark.functionality
def test_an_enrolled_person_s_message_is_answered_in_their_conversation_with_their_own_rights(world):
    assert forward(text("m1", "Quelles factures ?")) == [200]
    assert world.events == [
        ("start", "Quelles factures ?", {"email": "amal@cabinet.ma", "admin": False}),
        ("end", "Quelles factures ?"),
        ("send", RECIPIENT, "Bonjour Amal"),
    ]
    # the tools check them as they check an email's sender
    assert world.remembered == [("wa-session", "amal@cabinet.ma")]
    assert whatsapp.runs.WRITER.get() is None


@pytest.mark.functionality
def test_a_file_is_saved_before_the_run_that_reads_it(world):
    pdf = {"file": base64.b64encode(b"%PDF-1.4").decode(), "mimetype": "application/pdf", "filename": "f.pdf"}
    assert forward({**text("m1", ""), **pdf}) == [200]
    assert [event[0] for event in world.events] == ["save", "start", "end", "send"]
    assert world.events[0] == ("save", ["f.pdf"])


@pytest.mark.security
def test_a_message_sparrow_sends_twice_is_answered_once(world):
    assert forward(text("m1"), text("m1")) == [200, 200]
    assert [event[0] for event in world.events].count("start") == 1


@pytest.mark.security
def test_someone_who_is_no_longer_a_collaborator_gets_no_answer_and_no_run(world):
    world.collaborator = False
    assert forward(text("m1")) == [200]
    assert world.events == [] and world.remembered == []


@pytest.mark.functionality
def test_a_file_tarik_does_not_take_is_refused_in_words_with_no_run(world):
    exe = {"file": base64.b64encode(b"MZ").decode(), "mimetype": "application/x-msdownload", "filename": "x.exe"}
    assert forward({**text("m1", ""), **exe}) == [200]
    assert [event[0] for event in world.events] == ["send"]
    assert "x.exe" in world.events[0][2] and "type non accepté" in world.events[0][2]


@pytest.mark.functionality
def test_a_person_s_messages_are_answered_one_after_the_other(world):
    world.pause = 0.05
    assert forward(text("m1", "un"), text("m2", "deux")) == [200, 200]
    assert [event[:2] for event in world.events if event[0] != "send"] == [
        ("start", "un"),
        ("end", "un"),
        ("start", "deux"),
        ("end", "deux"),
    ]


@pytest.mark.functionality
def test_a_failed_run_tells_the_person_once_to_send_again(world):
    world.reply = RuntimeError("model down")
    assert forward(text("m1")) == [200]
    assert world.events == [("send", RECIPIENT, whatsapp.BUSY)]
    world.events.clear()
    world.reply = {}  # a run that ended with neither reply nor error
    assert forward(text("m2")) == [200]
    assert world.events[-1] == ("send", RECIPIENT, whatsapp.ERROR)


@pytest.mark.functionality
def test_a_message_that_cannot_be_read_is_refused(world):
    assert forward(None, raw=b"not json") == [400]
    assert forward({"content": "Bonjour"}) == [400]  # no message_id
    assert forward({**text("m1"), "file": "***not base64***"}) == [400]
    assert world.events == []
