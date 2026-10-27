"""The Routines page: the routines of the cabinet picked in the sidebar only, each change checked as Tarik's routine
tool checks it, and journaled."""

import pytest

from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import routines
from server import api
from tests.api.test_settings import asked

ROUTINE = {"enabled": True, "kind": "instruction", "every": "week", "days": [0], "hour": 9}
ROUTINE |= {"notify": "chef@cabinet.ma", "prompt": "Liste les factures en brouillon."}


@pytest.fixture
def page(db_cabinet, monkeypatch):
    """The page's handler on the test cabinet (addresses @cabinet.ma allowed, Chef a live collaborator); the
    journal's entries."""
    journal = []

    async def record(actor, action, target="", **detail):
        journal.append((actor, action, target, detail))

    monkeypatch.setattr(api.routines, "chosen", lambda request: db_cabinet)
    monkeypatch.setattr(routines, "recipient_allowed", lambda address: address.endswith("@cabinet.ma"))
    monkeypatch.setattr(routines, "live", lambda address: address == "chef@cabinet.ma")
    monkeypatch.setattr(api.routines.audit, "record", record)
    return db_cabinet, journal


def post(cabinet, action, routine):
    return asked(api.routines.routines_page, "POST", {"cabinet": cabinet, "action": action, "routine": routine})


@pytest.mark.functionality
def test_a_routine_made_changed_and_deleted_from_the_page(page):
    cabinet, journal = page
    status, data, _ = post(cabinet, "create", ROUTINE)
    assert status == 200 and data["error"] is None
    (made,) = data["routines"]
    assert made["created_by"] == "a@experio.ma" and made["schedule"] == "chaque lundi à 9 h" and made["enabled"]
    changed = {**ROUTINE, "id": made["id"], "enabled": False, "every": "month", "days": [5], "prompt": "Autre."}
    status, data, _ = post(cabinet, "save", changed)
    (saved,) = data["routines"]
    assert not saved["enabled"] and saved["schedule"] == "chaque mois, le 5 à 9 h"
    assert saved["prompt"] == "Autre."
    status, data, _ = post(cabinet, "delete", {"id": made["id"]})
    assert data["routines"] == [] and [entry[3]["change"] for entry in journal] == ["create", "save", "delete"]


@pytest.mark.security
@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"notify": "moi@gmail.com"}, "pas dans les adresses autorisées"),
        ({"notify": "client@cabinet.ma"}, "n'est ni un administrateur actif du cabinet"),
        ({"prompt": ""}, "quelle consigne"),
        ({"kind": "tva", "hour": 0}, "de 1 à 23"),
        ({"hour": "huit"}, "illisible"),
    ],
)
def test_a_routine_the_tool_would_refuse_is_refused_by_the_page(page, change, said):
    cabinet, journal = page
    status, data, _ = post(cabinet, "create", {**ROUTINE, **change})
    assert status == 400 and said in data["error"] and data["routines"] == [] and journal == []


@pytest.mark.security
def test_a_page_opened_on_another_cabinet_changes_nothing(page):
    _, journal = page
    status, data, _ = post("cabinet_b", "create", ROUTINE)
    assert status == 400 and "un autre cabinet" in data["error"] and journal == []
    with cabinet_db() as db:
        assert db.execute("select count(*) from tarik_routines").fetchone() == (0,)


@pytest.mark.functionality
def test_a_change_keeps_its_next_run_unless_its_schedule_moves(page):
    cabinet, _ = page
    _, data, _ = post(cabinet, "create", ROUTINE)
    (made,) = data["routines"]
    with cabinet_db() as db:
        db.execute("update tarik_routines set next_run_at = now() - interval '1 minute'")
        (due,) = db.execute("select next_run_at from tarik_routines").fetchone()
    status, data, _ = post(cabinet, "save", {**ROUTINE, "id": made["id"], "prompt": "Autre."})
    with cabinet_db() as db:
        assert db.execute("select next_run_at from tarik_routines").fetchone() == (due,)  # still due: not skipped
    assert status == 200 and data["error"] is None
    post(cabinet, "save", {**ROUTINE, "id": made["id"], "hour": 10})
    with cabinet_db() as db:
        assert db.execute("select next_run_at from tarik_routines").fetchone()[0] > due  # a new hour: rescheduled


@pytest.mark.functionality
def managers_asked():
    """The page's second request: GET /ui/api/routines?managers, as an admin."""
    import asyncio
    import json

    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": "/ui/api/routines", "headers": [], "query_string": b"managers"}
    request = Request(scope, receive)
    request.state.user = {"email": "a@experio.ma", "name": "A", "role": "admin"}
    return json.loads(asyncio.run(api.routines.routines_page(request)).body)


@pytest.mark.functionality
def test_the_page_shows_at_once_and_asks_the_cabinets_admins_apart_kept_a_while(page, monkeypatch):
    monkeypatch.setattr(api.routines, "MANAGERS", {})
    calls = []

    def admins():
        calls.append(1)
        return [{"name": "Chef", "email": "chef@cabinet.ma"}]

    monkeypatch.setattr(api.routines, "admins", admins)
    status, data, _ = asked(api.routines.routines_page)
    assert status == 200 and "managers" not in data and calls == []  # the page never waits on Experio
    assert managers_asked()["managers"] == [{"name": "Chef", "email": "chef@cabinet.ma"}]
    assert managers_asked()["managers"] and calls == [1]  # kept: Experio asked once


@pytest.mark.functionality
def test_without_experio_the_address_is_typed(page, monkeypatch):
    monkeypatch.setattr(api.routines, "MANAGERS", {})

    def down():
        raise ConnectionRefusedError("Experio down")

    monkeypatch.setattr(api.routines, "admins", down)
    assert managers_asked()["managers"] is None


@pytest.mark.functionality
def test_a_routine_on_several_weekdays(page):
    cabinet, _ = page
    status, data, _ = post(cabinet, "create", {**ROUTINE, "days": [4, 0, 2, 0]})
    assert status == 200 and data["routines"][0]["days"] == [0, 2, 4]
    assert data["routines"][0]["schedule"] == "chaque lundi, mercredi et vendredi à 9 h"
    status, data, _ = post(cabinet, "create", {**ROUTINE, "days": []})
    assert status == 400 and "quels jours" in data["error"]
