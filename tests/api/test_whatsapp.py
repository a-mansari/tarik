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
        self.looked = []  # the recipient_ids looked up in the database
        self.taken = set()
        self.collaborator = True
        self.outcome = ({"reply": "Bonjour Amal"}, None)  # what the run ends with, or the exception its start raises
        self.stopped = False
        self.pause = 0  # seconds a run lasts
        self.send_pause = 0  # seconds a send lasts
        self.send_error = None  # what every send raises
        self.events = []  # ("save", names) ("start", message, writer) ("end", message) ("send", recipient, text)
        self.remembered = []


@pytest.fixture
def world(monkeypatch):
    world = World()
    monkeypatch.setattr(whatsapp, "CABINETS", frozenset({"cabinet_a"}))

    async def recipient_of(recipient_id):
        world.looked.append(recipient_id)
        return world.enrolled.get(recipient_id)

    async def first_time(message_id):
        fresh = message_id not in world.taken
        world.taken.add(message_id)
        return fresh

    async def limited(cabinet, function, /, *args):
        assert cabinet == "cabinet_a"
        if function is people.collaborator:
            return world.collaborator
        if function is people.remember_whatsapp_sender:
            return world.remembered.append(args)
        if function is whatsapp.sparrow.send:
            await asyncio.sleep(world.send_pause)
            if world.send_error:
                raise world.send_error
            return world.events.append(("send", *args))
        raise AssertionError(function)

    async def call(cabinet, tool, **arguments):
        world.events.append(("save", [f["file_name"] for f in arguments["files"]]))

    async def start(name, payload):
        assert name == "discussion" and payload["session_id"] == "wa-session"
        message = payload["input"]["message"]
        if isinstance(world.outcome, Exception):
            raise world.outcome
        world.events.append(("start", message, whatsapp.runs.WRITER.get()))

        async def finish():
            await asyncio.sleep(world.pause)
            if world.stopped:
                raise asyncio.CancelledError  # runs.stop cancelled the run's task
            world.events.append(("end", message))
            return world.outcome

        return "run-1", asyncio.ensure_future(finish())

    monkeypatch.setattr(whatsapp, "recipient_of", recipient_of)
    monkeypatch.setattr(whatsapp, "first_time", first_time)
    monkeypatch.setattr(whatsapp, "limited", limited)
    monkeypatch.setattr(whatsapp, "call", call)
    monkeypatch.setattr(whatsapp.runs, "start", start)
    return world


def forward(*messages, bearer=RECIPIENT, raw=None, chunks=None):
    """Post each of ``messages`` (dicts; or ``raw`` bytes, or ``chunks`` of bytes with no content-length) as Sparrow
    does and return the statuses, once every answer started has ended."""

    def receiver(parts):
        queue = list(parts)

        async def receive():
            return {"type": "http.request", "body": queue.pop(0), "more_body": bool(queue)}

        return receive

    async def main():
        statuses = []
        for message in messages:
            parts = chunks or [raw if raw is not None else json.dumps(message).encode()]
            headers = [(b"authorization", f"Bearer {bearer}".encode())] if bearer is not None else []
            answer = await api.whatsapp.handler(Request({"type": "http", "headers": headers}, receiver(parts)))
            statuses.append(answer.status_code)
        await asyncio.gather(*whatsapp.TASKS)
        return statuses

    return asyncio.run(main())


def text(message_id, content="Bonjour"):
    return {"message_id": message_id, "reply_to_message_id": None, "content": content}


def file(name, mime, data=b"%PDF-1.4"):
    return {"file": base64.b64encode(data).decode(), "mimetype": mime, "filename": name}


def sent(world):
    return [event[2] for event in world.events if event[0] == "send"]


@pytest.mark.security
@pytest.mark.parametrize("bearer", ["f" * 16, "", "short", "r" * 17, None])
def test_a_message_from_nobody_enrolled_is_refused_and_nothing_runs(world, bearer):
    assert forward(text("m1"), bearer=bearer) == [401]
    assert world.events == [] and world.taken == set()
    assert world.looked in ([], ["f" * 16])  # a bearer not of the form Sparrow makes costs no database query


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
    assert forward({**text("m1", ""), **file("f.pdf", "application/pdf")}) == [200]
    assert [event[0] for event in world.events] == ["save", "start", "end", "send"]
    assert world.events[0] == ("save", ["f.pdf"])


@pytest.mark.functionality
def test_a_document_named_without_an_extension_takes_the_one_its_mimetype_gives(world):
    assert forward({**text("m1", ""), **file("Facture mars", "application/pdf")}) == [200]
    assert world.events[0] == ("save", ["Facture mars.pdf"])  # as Experio's own handler names it
    world.events.clear()
    assert forward({**text("m2", ""), **file("1234", "image/jpeg")}) == [200]
    assert world.events[0][1][0].startswith("1234.jp")


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
    assert forward({**text("m1", ""), **file("x.exe", "application/x-msdownload", b"MZ")}) == [200]
    assert [event[0] for event in world.events] == ["send"]
    assert "x.exe" in sent(world)[0] and "type non accepté" in sent(world)[0]


@pytest.mark.functionality
def test_an_empty_message_is_told_so_not_blamed_on_an_archive(world):
    assert forward(text("m1", "  ")) == [200]
    assert sent(world) == [whatsapp.UNREADABLE] and [e for e in world.events if e[0] == "start"] == []


@pytest.mark.functionality
def test_a_person_s_messages_are_answered_one_after_the_other_replies_included(world):
    world.pause, world.send_pause = 0.05, 0.05
    assert forward(text("m1", "un"), text("m2", "deux")) == [200, 200]
    assert [event[:2] for event in world.events] == [
        ("start", "un"),
        ("end", "un"),
        ("send", RECIPIENT),
        ("start", "deux"),
        ("end", "deux"),
        ("send", RECIPIENT),
    ]


@pytest.mark.functionality
def test_a_run_that_fails_or_never_starts_tells_the_person_once_to_send_again(world):
    world.outcome = RuntimeError("model down")  # the start itself raises
    assert forward(text("m1")) == [200]
    assert sent(world) == [whatsapp.BUSY]
    world.events.clear()
    world.outcome = (None, RuntimeError("the run failed"))  # what runs.start's task really returns on a failure
    assert forward(text("m2")) == [200]
    assert sent(world)[-1] == whatsapp.ERROR


@pytest.mark.functionality
def test_a_run_stopped_by_hand_is_told_to_the_person_not_left_in_silence(world):
    world.stopped = True
    assert forward(text("m1")) == [200]
    assert sent(world) == [whatsapp.STOPPED]


@pytest.mark.functionality
def test_a_reply_that_reached_them_in_part_is_not_followed_by_a_second_apology(world):
    error = whatsapp.sparrow.SparrowError("Sparrow answered 502", 502)
    error.sent = ["part one"]
    world.send_error = error
    assert forward(text("m1")) == [200]
    assert sent(world) == []  # nothing recorded: no BUSY got through either, none was tried after the partial send
    assert [event[0] for event in world.events] == ["start", "end"]


@pytest.mark.functionality
def test_a_message_too_heavy_or_unreadable_is_told_in_words_since_sparrow_sends_it_no_more(world, monkeypatch):
    monkeypatch.setattr(whatsapp, "CAP", 200)
    assert forward(None, chunks=[b"x" * 150, b"x" * 150]) == [200]  # counted as it arrives, no header to trust
    assert sent(world) == [whatsapp.TOO_LARGE]
    world.events.clear()
    for body in (b"not json", json.dumps({"content": "Bonjour"}).encode()):  # no message_id
        assert forward(None, raw=body) == [200]
    for bad in (None, "", "i" * 129, 7):
        assert forward({"message_id": bad, "content": "Bonjour"}) == [200]
    assert forward({**text("m1"), "file": "***not base64***"}) == [200]
    assert sent(world) == [whatsapp.UNREADABLE] * 7
    assert [event for event in world.events if event[0] in ("start", "save")] == []
    assert world.taken == set()
