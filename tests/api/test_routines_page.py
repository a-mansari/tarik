"""The Routines page: the routines of the cabinet picked in the sidebar only, each change checked as Tarik's routine
tool checks it, and journaled."""

import pytest

from engine.tools.core.cabinets import cabinet_db
from server import api
from tests.api.test_settings import asked

ROUTINE = {"enabled": True, "kind": "instruction", "dossier_ids": [5], "every": "week", "day": 0, "hour": 9}
ROUTINE |= {"notify": "chef@cabinet.ma", "prompt": "Liste les factures en brouillon."}


@pytest.fixture
def page(db_cabinet, monkeypatch):
    """The page's handler on the test cabinet (dossiers 5 ALPHA and 6 BETA, addresses @cabinet.ma allowed, Chef a
    live collaborator who has dossier 5 only); the journal's entries."""
    journal = []

    async def record(actor, action, target="", **detail):
        journal.append((actor, action, target, detail))

    monkeypatch.setattr(api.routines, "chosen", lambda request: db_cabinet)
    monkeypatch.setattr(api.routines, "dossiers", lambda: [{"id": 5, "name": "ALPHA"}, {"id": 6, "name": "BETA"}])
    monkeypatch.setattr(api.routines, "recipient_allowed", lambda address: address.endswith("@cabinet.ma"))
    monkeypatch.setattr(api.routines.audit, "record", record)
    monkeypatch.setattr(
        api.routines,
        "allowed_dossiers",
        lambda notify, ids: [d for d in ids if d == 5] if notify == "chef@cabinet.ma" else None,
    )
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
    changed = {**ROUTINE, "id": made["id"], "enabled": False, "every": "month", "day": 5, "prompt": "Autre consigne."}
    status, data, _ = post(cabinet, "save", changed)
    (saved,) = data["routines"]
    assert not saved["enabled"] and saved["schedule"] == "chaque mois, le 5 à 9 h"
    assert saved["prompt"] == "Autre consigne."
    status, data, _ = post(cabinet, "delete", {"id": made["id"]})
    assert data["routines"] == [] and [entry[3]["change"] for entry in journal] == ["create", "save", "delete"]


@pytest.mark.security
@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"notify": "moi@gmail.com"}, "pas dans les adresses autorisées"),
        ({"dossier_ids": [5, 6]}, "n'a pas, dans Experio, tous les dossiers choisis"),
        ({"notify": "client@cabinet.ma"}, "n'est pas un collaborateur actif d'Experio"),
        ({"dossier_ids": []}, "pour quels dossiers"),
        ({"prompt": ""}, "quelle consigne"),
        ({"kind": "tva", "dossier_ids": [5], "hour": 0}, "de 1 à 23"),
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
def test_a_change_keeps_its_next_run_and_its_closed_dossiers_unless_its_schedule_moves(page):
    cabinet, _ = page
    _, data, _ = post(cabinet, "create", ROUTINE)
    (made,) = data["routines"]
    with cabinet_db() as db:  # BETA (6) on it since, then taken from Chef: it may stay, its runs leave it out
        db.execute("update tarik_routines set dossier_ids = '{5,6}', next_run_at = now() - interval '1 minute'")
        (due,) = db.execute("select next_run_at from tarik_routines").fetchone()
    status, data, _ = post(cabinet, "save", {**ROUTINE, "id": made["id"], "dossier_ids": [5, 6], "prompt": "Autre."})
    with cabinet_db() as db:
        assert db.execute("select next_run_at from tarik_routines").fetchone() == (due,)  # still due: not skipped
    assert status == 200 and data["error"] is None
    post(cabinet, "save", {**ROUTINE, "id": made["id"], "dossier_ids": [5, 6], "hour": 10})
    with cabinet_db() as db:
        assert db.execute("select next_run_at from tarik_routines").fetchone()[0] > due  # a new hour: rescheduled


@pytest.mark.functionality
def test_a_change_never_waits_on_experios_whole_dossier_list(page, monkeypatch):
    cabinet, _ = page
    monkeypatch.setattr(api.routines, "dossiers", lambda: pytest.fail("the whole list is read for the page only"))
    status, data, _ = post(cabinet, "create", ROUTINE)
    assert status == 200 and "dossiers" not in data
