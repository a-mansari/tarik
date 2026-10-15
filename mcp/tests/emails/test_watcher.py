"""The mailbox watcher: which cabinets it watches, and that each wake-up names its own cabinet."""

import pytest

import watcher
from tarik.core import cabinets


@pytest.fixture
def three(monkeypatch):
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"tarik_test_a", "tarik_test_b", "tarik_test_c"}))
    monkeypatch.setattr(watcher, "CABINETS", cabinets.CABINETS)
    monkeypatch.setenv("TARIK_TEST_A__IMAP_USER", "tarik@a.ma")
    monkeypatch.setenv("TARIK_TEST_C__IMAP_USER", "tarik@c.ma")


@pytest.mark.functionality
def test_only_cabinets_with_their_own_mailbox_are_watched(three):
    assert watcher.mailboxes() == ["tarik_test_a", "tarik_test_c"]


@pytest.mark.security
def test_a_wake_up_names_its_cabinet_and_carries_the_secret(three, monkeypatch):
    sent = []
    monkeypatch.setenv("MAIL_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(watcher, "HTTP", type("Http", (), {"post": lambda self, url, json, timeout, headers: sent.append((json, headers))})())
    with cabinets.using("tarik_test_c"):
        watcher.wake_tarik("tarik_test_c", "")
    assert sent == [({"cabinet": "tarik_test_c"}, {"X-Tarik-Webhook": "s3cret"})]


@pytest.mark.security
def test_each_watch_runs_in_its_own_cabinet(three, monkeypatch):
    """The IDLE loop logs into the cabinet it watches: its own IMAP_USER, never another's."""
    logins = []

    class Seen(BaseException):  # not an Exception: the watch loop retries those, forever
        pass

    def connect():
        logins.append(cabinets.setting("IMAP_USER"))
        raise Seen  # one login is enough to see whose
    monkeypatch.setattr(watcher, "connect", connect)
    for cabinet in ("tarik_test_a", "tarik_test_c"):
        with pytest.raises(Seen):
            watcher.watch(cabinet)
    assert logins == ["tarik@a.ma", "tarik@c.ma"]
