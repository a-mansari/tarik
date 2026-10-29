"""How Tarik behaves for a cabinet: each setting's default, a change checked then kept with who made it and written
in the Journal, made only by an admin, and the acknowledgement left unsent once the cabinet turns it off."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.dossiers import behaviour
from engine.tools.emails import inbox


@pytest.fixture
def journal(db_cabinet, monkeypatch):
    """The Journal's entries (the orchestrator's table is not in the test database): (author, key, via)."""
    entries = []
    monkeypatch.setattr(behaviour, "journaled", lambda author, key, via: entries.append((author, key, via)))
    return entries


@pytest.fixture
def by_email(journal, monkeypatch):
    """The conversation is an email of chef@cabinet.ma, an Experio admin of the cabinet (``reach`` changes that)."""
    reach = {"rank": behaviour.RANK["admin"]}
    monkeypatch.setattr(behaviour, "sender_of", lambda session_id: "chef@cabinet.ma")
    monkeypatch.setattr(behaviour, "sender_reach", lambda session_id: reach)
    return reach


@pytest.mark.functionality
def test_a_setting_never_changed_has_its_default(db_cabinet):
    assert behaviour.behaviour("acknowledge") is True and behaviour.behaviour("progress") == 0
    assert all(found["changed_by"] is None for found in behaviour.settings())


@pytest.mark.functionality
def test_an_admin_changes_a_setting_by_email_and_the_journal_says_so(by_email, journal):
    assert behaviour.manage_behaviour("s1", "update", "progress", "10")["status"] == "ok"
    assert behaviour.manage_behaviour("s1", "update", "acknowledge", "non")["message"].endswith("réception : non.")
    assert behaviour.behaviour("progress") == 10 and behaviour.behaviour("acknowledge") is False
    assert {found["changed_by"] for found in behaviour.settings()} == {"chef@cabinet.ma"}
    assert journal == [("chef@cabinet.ma", key, "conversation s1") for key in ("progress", "acknowledge")]
    listed = behaviour.manage_behaviour("s1")["message"]
    assert "Accusé de réception : non" in listed and "Point d'avancement (minutes) : 10" in listed


@pytest.mark.security
def test_a_collaborator_who_is_no_admin_changes_nothing_by_email(by_email, journal):
    by_email["rank"] = behaviour.RANK["approver"]
    assert behaviour.manage_behaviour("s1", "update", "acknowledge", "non")["status"] == "stop"
    assert behaviour.behaviour("acknowledge") is True and journal == []
    assert behaviour.manage_behaviour("s1")["status"] == "ok"  # listing them stays open


@pytest.mark.security
def test_in_the_chat_only_a_tarik_admin_changes_a_setting(journal):
    member, admin = {"email": "membre@cabinet.ma", "admin": False}, {"email": "admin@cabinet.ma", "admin": True}
    for writer in (None, member):  # a chat run without its writer (a script's), or a member's
        assert behaviour.manage_behaviour("c1", "update", "acknowledge", "non", writer)["status"] == "stop"
    assert behaviour.manage_behaviour("c1", "update", "acknowledge", "non", admin)["status"] == "ok"
    assert journal == [("admin@cabinet.ma", "acknowledge", "conversation c1")]


@pytest.mark.functionality
def test_a_delay_kept_before_its_maximum_was_lowered_counts_as_the_maximum(db_cabinet):
    with behaviour.cabinet_db() as db:
        db.execute(behaviour.Q.behaviour_set, ("progress", behaviour.Jsonb(60), "chef@cabinet.ma"))
    assert behaviour.behaviour("progress") == behaviour.MAX_MINUTES


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("key", "value"),
    [("acknowledge", "peut-être"), ("progress", "-1"), ("progress", "26"), ("progress", "²"), ("x", "1")],
)
def test_a_wrong_value_changes_nothing(by_email, journal, key, value):
    with pytest.raises(ToolError):
        behaviour.change(key, value, "chef@cabinet.ma")
    assert behaviour.manage_behaviour("s1", "update", key, value)["message"].startswith("Rien n'est changé : ")
    assert all(found["changed_by"] is None for found in behaviour.settings()) and journal == []


@pytest.mark.communication
def test_an_acknowledgement_turned_off_is_not_sent(journal, monkeypatch):
    sent = []
    monkeypatch.setattr(inbox, "send_email", lambda *given: sent.append(given))
    behaviour.change("acknowledge", "non", "chef@cabinet.ma")
    assert journal == [("chef@cabinet.ma", "acknowledge", "page")]
    assert inbox.acknowledge_thread("<m1@x>")["sent"] is False and sent == []


@pytest.mark.security
def test_a_chat_runs_writer_reaches_the_tool_from_the_run_never_from_the_model():
    from engine import agent, runs

    assert "writer" not in agent.reglages.args  # the model cannot name one
    token = runs.WRITER.set({"email": "admin@cabinet.ma", "admin": True})
    try:
        assert runs.configurable_of("c", "s", None)["writer"] == {"email": "admin@cabinet.ma", "admin": True}
    finally:
        runs.WRITER.reset(token)
    assert "writer" not in runs.configurable_of("c", "s", None)
