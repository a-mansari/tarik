"""How Tarik behaves for a cabinet: each setting's default, a change checked then kept with who made it, and the
acknowledgement left unsent once the cabinet turns it off."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.dossiers import behaviour
from engine.tools.emails import inbox


@pytest.mark.functionality
def test_a_setting_never_changed_has_its_default(db_cabinet):
    assert behaviour.behaviour("acknowledge") is True and behaviour.behaviour("progress") == 0
    assert all(found["changed_by"] is None for found in behaviour.settings())


@pytest.mark.functionality
def test_a_manager_changes_a_setting_in_a_conversation(db_cabinet, monkeypatch):
    monkeypatch.setattr(behaviour, "sender_of", lambda session_id: "chef@cabinet.ma")
    assert behaviour.manage_behaviour("s1", "update", "progress", "10")["status"] == "ok"
    assert behaviour.manage_behaviour("s1", "update", "acknowledge", "non")["status"] == "ok"
    assert behaviour.behaviour("progress") == 10 and behaviour.behaviour("acknowledge") is False
    assert {found["changed_by"] for found in behaviour.settings()} == {"chef@cabinet.ma"}
    listed = behaviour.manage_behaviour("s1")["message"]
    assert "Accusé de réception : non" in listed and "Point d'avancement (minutes) : 10" in listed


@pytest.mark.security
def test_only_an_admin_changes_a_setting_by_email(db_cabinet, monkeypatch):
    rank = {"rank": behaviour.RANK["approver"]}
    monkeypatch.setattr(behaviour, "sender_reach", lambda session_id: rank)
    assert behaviour.manage_behaviour("s1", "update", "acknowledge", "non")["status"] == "stop"
    assert behaviour.behaviour("acknowledge") is True and behaviour.manage_behaviour("s1")["status"] == "ok"
    rank["rank"] = behaviour.RANK["admin"]
    assert behaviour.manage_behaviour("s1", "update", "acknowledge", "non")["status"] == "ok"


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("key", "value"),
    [("acknowledge", "peut-être"), ("progress", "-1"), ("progress", "121"), ("progress", "²"), ("x", 1)],
)
def test_a_wrong_value_changes_nothing(db_cabinet, key, value):
    with pytest.raises(ToolError):
        behaviour.change(key, value, "chef@cabinet.ma")
    assert behaviour.manage_behaviour("s1", "update", key, str(value))["status"] == "stop"
    assert all(found["changed_by"] is None for found in behaviour.settings())


@pytest.mark.communication
def test_an_acknowledgement_turned_off_is_not_sent(db_cabinet, monkeypatch):
    sent = []
    monkeypatch.setattr(inbox, "send_email", lambda *given: sent.append(given))
    behaviour.change("acknowledge", False, "chef@cabinet.ma")
    assert inbox.acknowledge_thread("<m1@x>")["sent"] is False and sent == []
