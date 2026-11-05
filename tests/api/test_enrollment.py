"""The WhatsApp page: a collaborator is enrolled with their Experio phone, once, only as a collaborator, and removed
again from Sparrow before their row goes."""

import asyncio
import json

import pytest
from psycopg.errors import UniqueViolation
from starlette.requests import Request

from engine.tools.core import cabinets
from engine.tools.core.queries import Q
from engine.tools.dossiers import people
from server import api
from server.api import enrollment

AMAL = {"login": "amal@cabinet.ma", "name": "Amal", "phone": "0600600600", "cabinet_name": "Cabinet Alami"}
ME = {"email": "admin@experio.ma", "role": "admin"}
ENROLL = {"action": "enroll", "login": "amal@cabinet.ma", "phone": "0600600600"}  # the phone the page showed


class Database:
    """The orchestrator's tables as the page uses them, faked: rows (recipient_id, cabinet, email, phone, session)."""

    def __init__(self):
        self.rows = []
        self.events = []  # "row deleted"
        self.fail_insert = None  # the exception the insert raises

    def connection(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=()):
        rows = []
        if query == Q.whatsapp_person:
            rows = [(r[0],) for r in self.rows if (r[1], r[2].lower()) == (params[0], params[1].lower())]
        elif query == Q.whatsapp_enrolled:
            rows = [(r[2], r[3]) for r in self.rows if r[1] == params[0]]
        elif query == Q.whatsapp_enrolling:
            if self.fail_insert:
                raise self.fail_insert
            self.rows.append(tuple(params))
        elif query == Q.whatsapp_unenrolled:
            self.rows = [r for r in self.rows if r[0] != params[0]]
            self.events.append("row deleted")
        else:
            raise AssertionError(query)
        return Result(rows)


class Result:
    def __init__(self, rows):
        self.rows = rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class World:
    def __init__(self):
        self.db = Database()
        self.person = dict(AMAL)
        self.candidates = [{k: AMAL[k] for k in ("login", "name", "phone")}]
        self.sparrow = None  # an exception sparrow.enroll/remove raise
        self.remove_error = None  # an exception only sparrow.remove raises
        self.listing_fails = False
        self.calls = []  # ("enroll", phone, host, name, cabinet_name) ("remove", recipient_id)
        self.audited = []
        self.events = self.db.events


@pytest.fixture
def world(monkeypatch):
    world = World()
    monkeypatch.setattr(api.runs, "CABINETS", frozenset({"cabinet_a", "cabinet_b"}))
    monkeypatch.setattr(enrollment.runs, "pool", world.db)

    async def limited(cabinet, function, /, *args):
        assert cabinet == "cabinet_a"
        if function is people.whatsapp_candidates:
            if world.listing_fails:
                raise RuntimeError("Experio down")
            return world.candidates
        if function is enrollment.person_to_enroll:
            if world.person is None:
                raise ValueError(f"{args[0]} n'est pas un collaborateur de ce cabinet")
            return world.person
        if function is enrollment.public_host:
            return "tarik.example"
        if function is enrollment.sparrow.enroll:
            world.calls.append(("enroll", *args))
            if world.sparrow:
                raise world.sparrow
            return "r" * 16
        if function is enrollment.sparrow.remove:
            world.calls.append(("remove", *args))
            world.events.append("sparrow remove")
            if world.sparrow or world.remove_error:
                raise world.sparrow or world.remove_error
            return None
        raise AssertionError(function)

    async def record(actor, action, target="", **detail):
        world.audited.append((actor, action, target, detail))

    monkeypatch.setattr(enrollment, "limited", limited)
    monkeypatch.setattr(enrollment.audit, "record", record)
    return world


async def posted_async(form, cabinet="cabinet_a", shown=None):
    """``form`` posted by an admin's page of ``shown`` (default ``cabinet``), ``cabinet`` picked in the sidebar."""
    body = json.dumps({"cabinet": shown or cabinet, **form}).encode()

    async def receive():
        return {"type": "http.request", "body": body}

    headers = [(b"cookie", f"tarik_cabinet={cabinet}".encode())]
    scope = {"type": "http", "method": "POST", "headers": headers, "state": {"user": ME}}
    answer = await enrollment.enrollment_page(Request(scope, receive))
    return answer.status_code, json.loads(answer.body)


def posted(form, cabinet="cabinet_a", shown=None):
    return asyncio.run(posted_async(form, cabinet, shown))


def listed(cabinet="cabinet_a"):
    scope = {"type": "http", "method": "GET", "headers": [(b"cookie", f"tarik_cabinet={cabinet}".encode())]}
    answer = asyncio.run(enrollment.enrollment_page(Request(scope)))
    return answer.status_code, json.loads(answer.body)


@pytest.mark.functionality
def test_a_collaborator_is_enrolled_with_their_experio_phone_at_tarik_s_own_host(world):
    status, body = posted(ENROLL)
    assert status == 200 and "Amal peut écrire à Tarik" in body["done"]
    assert world.calls == [("enroll", "212600600600", "tarik.example", "Amal", "Cabinet Alami")]
    ((recipient_id, cabinet, login, phone, session),) = world.db.rows
    assert (recipient_id, cabinet, login, phone) == ("r" * 16, "cabinet_a", "amal@cabinet.ma", "212600600600")
    assert session.startswith("wa-") and len(session) == 35  # a conversation of its own, never a mail thread's id
    assert world.audited == [("admin@experio.ma", "whatsapp_enrolled", "amal@cabinet.ma", {"cabinet": "cabinet_a"})]
    assert body["people"][0]["state"] == "enrolled"


@pytest.mark.functionality
def test_someone_already_enrolled_is_not_enrolled_twice(world):
    posted(ENROLL)
    world.calls.clear()
    status, body = posted(ENROLL)
    assert status == 400 and "déjà activé" in body["error"] and world.calls == []


@pytest.mark.security
def test_nothing_reaches_sparrow_for_someone_who_is_no_collaborator_or_has_no_usable_phone(world):
    world.person = None
    status, body = posted({**ENROLL, "login": "client@societe.ma"})
    assert status == 400 and "pas un collaborateur" in body["error"]
    world.person = {**AMAL, "phone": "+33 6 12 34 56 78"}
    status, body = posted(ENROLL)
    assert status == 400 and "pas un numéro marocain" in body["error"]
    assert world.calls == [] and world.db.rows == [] and world.audited == []


@pytest.mark.functionality
def test_a_phone_sparrow_already_holds_is_told_in_words_and_nothing_is_kept(world):
    world.sparrow = enrollment.sparrow.PhoneTaken("212600600600 is already a Sparrow recipient")
    status, body = posted(ENROLL)
    assert status == 400 and "déjà enregistré chez Sparrow" in body["error"]
    assert world.db.rows == [] and world.audited == []


@pytest.mark.functionality
def test_what_sparrow_registered_is_removed_again_when_the_row_cannot_be_kept(world):
    world.db.fail_insert = RuntimeError("database down")
    with pytest.raises(RuntimeError):
        posted(ENROLL)
    assert world.calls[-1] == ("remove", "r" * 16) and world.audited == []


@pytest.mark.functionality
def test_a_person_is_removed_from_sparrow_before_their_row_goes(world):
    posted(ENROLL)
    status, body = posted({"action": "remove", "login": "amal@cabinet.ma"})
    assert status == 200 and world.calls[-1] == ("remove", "r" * 16) and world.db.rows == []
    assert world.events == ["sparrow remove", "row deleted"]  # Sparrow first, the row after
    assert world.audited[-1][1] == "whatsapp_removed"
    status, body = posted({"action": "remove", "login": "amal@cabinet.ma"})
    assert status == 400 and "n'est pas activé" in body["error"]


@pytest.mark.functionality
def test_a_person_sparrow_cannot_remove_keeps_their_row(world):
    posted(ENROLL)
    world.sparrow = enrollment.sparrow.SparrowError("Sparrow answered 500", 500)
    status, body = posted({"action": "remove", "login": "amal@cabinet.ma"})
    assert status == 400 and len(world.db.rows) == 1 and world.audited[-1][1] == "whatsapp_enrolled"


@pytest.mark.security
def test_a_page_of_another_cabinet_than_the_one_picked_changes_nothing(world):
    status, body = posted(ENROLL, "cabinet_a", shown="cabinet_b")
    assert status == 400 and "autre cabinet" in body["error"]
    assert world.calls == [] and world.db.rows == []


@pytest.mark.functionality
def test_the_page_lists_each_collaborator_as_ready_enrolled_or_without_a_usable_phone(world):
    world.candidates = [
        {"login": "amal@cabinet.ma", "name": "Amal", "phone": "0600600600"},
        {"login": "karim@cabinet.ma", "name": "Karim", "phone": ""},
        {"login": "lea@cabinet.ma", "name": "Léa", "phone": "+33 6 12 34 56 78"},
        {"login": "sara@cabinet.ma", "name": "Sara", "phone": "0611111111"},
    ]
    world.db.rows = [
        ("s" * 16, "cabinet_a", "sara@cabinet.ma", "212611111111", "wa-1"),
        ("g" * 16, "cabinet_a", "gone@cabinet.ma", "212622222222", "wa-2"),  # no longer a collaborator
        ("o" * 16, "cabinet_b", "amal@cabinet.ma", "212600600600", "wa-3"),  # another cabinet's
    ]
    status, body = listed()
    states = {person["login"]: person["state"] for person in body["people"]}
    assert states == {
        "amal@cabinet.ma": "ready",
        "karim@cabinet.ma": "no_phone",
        "lea@cabinet.ma": "foreign",
        "sara@cabinet.ma": "enrolled",
        "gone@cabinet.ma": "enrolled",  # still listed, to be removed
    }


@pytest.mark.functionality
def test_tarik_s_public_host_is_the_gladden_callbacks(monkeypatch):
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"cabinet_a"}))
    monkeypatch.setenv("GLADDEN_CALLBACK_URL", "https://tarik.example/gladden")
    with cabinets.using("cabinet_a"):
        assert enrollment.public_host() == "tarik.example"  # no scheme, no path: Sparrow adds https:// and the route
        monkeypatch.delenv("GLADDEN_CALLBACK_URL")
        with pytest.raises(ValueError, match="adresse publique"):
            enrollment.public_host()


@pytest.mark.security
def test_a_phone_changed_in_experio_since_the_page_was_shown_is_never_enrolled(world):
    world.person = {**AMAL, "phone": "0611111111"}  # someone edited the user between the page and the click
    status, body = posted(ENROLL)
    assert status == 400 and "a changé dans Experio" in body["error"]
    assert posted({**ENROLL, "phone": ""})[0] == 400  # no phone posted: nothing guessed
    assert world.calls == [] and world.db.rows == []


@pytest.mark.functionality
def test_two_clicks_at_once_enroll_the_person_once(world):
    async def both():
        return await asyncio.gather(posted_async(ENROLL), posted_async(ENROLL))

    first, second = asyncio.run(both())
    assert sorted(status for status, _ in (first, second)) == [200, 400]
    assert [call[0] for call in world.calls] == ["enroll"] and len(world.db.rows) == 1
    assert "déjà activé" in next(body["error"] for status, body in (first, second) if status == 400)


@pytest.mark.functionality
def test_a_person_enrolled_by_another_server_meanwhile_is_told_and_what_was_made_is_removed(world):
    world.db.fail_insert = UniqueViolation("whatsapp_recipients_person")
    status, body = posted(ENROLL)
    assert status == 400 and "déjà activé" in body["error"]
    assert world.calls[-1] == ("remove", "r" * 16) and world.audited == []
    world.db.fail_insert = RuntimeError("database down")
    world.remove_error = enrollment.sparrow.SparrowError("Sparrow answered 500", 500)
    with pytest.raises(RuntimeError, match="database down"):  # the cleanup failing never hides the first error
        posted(ENROLL)


@pytest.mark.functionality
def test_a_person_is_found_whatever_the_case_of_the_login(world):
    posted(ENROLL)
    status, body = posted({"action": "remove", "login": "Amal@Cabinet.ma"})
    assert status == 200 and world.db.rows == []


@pytest.mark.functionality
def test_an_action_done_is_still_told_when_experio_cannot_be_read_afterwards(world):
    world.listing_fails = True
    status, body = posted(ENROLL)
    assert status == 503 and body["unreachable"] and "Amal peut écrire" in body["done"]
    assert len(world.db.rows) == 1  # it was done: the admin is not left to click again
