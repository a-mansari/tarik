"""The mailbox watcher (engine/mail.py): which cabinets it watches, and that each run it starts names its cabinet."""

import pytest

from engine import mail as watcher
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
def test_a_wake_up_starts_an_emails_run_for_its_own_cabinet(three, monkeypatch):
    import asyncio

    from engine import runs

    started = []

    async def start(graph, payload):
        started.append((graph, payload))
        return "run"

    monkeypatch.setattr(runs, "start", start)
    loop = asyncio.new_event_loop()
    thread = __import__("threading").Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(watcher, "LOOP", loop)
    with cabinets.using("tarik_test_c"):
        watcher.wake_tarik("tarik_test_c", "")
    loop.call_soon_threadsafe(loop.stop)
    assert started == [("emails", {"input": {"cabinet": "tarik_test_c"}, "cabinet": "tarik_test_c"})]


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


@pytest.mark.functionality
def test_the_workflow_is_woken_only_when_a_thread_waits(three, monkeypatch):
    woken, told = [], []
    monkeypatch.setattr(watcher, "read_mailbox", lambda: [{"sender": "x@gmail.com", "subject": "s", "reason": "r"}])
    monkeypatch.setattr(watcher, "notify_manager", lambda **alert: told.append(alert["sender"]))
    monkeypatch.setattr(watcher, "wake_tarik", lambda cabinet, why: woken.append(cabinet))
    with cabinets.using("tarik_test_a"):
        monkeypatch.setattr(watcher, "threads_waiting", lambda: False)
        watcher.serve("tarik_test_a", "new email")
        monkeypatch.setattr(watcher, "threads_waiting", lambda: True)
        watcher.serve("tarik_test_a", "new email")
    assert told == ["x@gmail.com", "x@gmail.com"] and woken == ["tarik_test_a"]


@pytest.mark.technical
def test_a_mailbox_that_cannot_be_read_still_wakes_the_waiting_threads(three, monkeypatch):
    woken = []
    monkeypatch.setattr(watcher, "read_mailbox", lambda: (_ for _ in ()).throw(ConnectionError("IMAP down")))
    monkeypatch.setattr(watcher, "threads_waiting", lambda: True)
    monkeypatch.setattr(watcher, "wake_tarik", lambda cabinet, why: woken.append(cabinet))
    with cabinets.using("tarik_test_a"):
        watcher.serve("tarik_test_a", "")
    assert woken == ["tarik_test_a"]


class Idling:
    """A mailbox whose IDLE hears ``heard`` ((kind, [data]) pairs), then times out."""

    def __init__(self, heard):
        self.heard = heard

    def idle(self, duration):
        heard = self.heard

        class Idler:
            def __enter__(self):
                return iter(heard)

            def __exit__(self, *exc):
                return False

        return Idler()


@pytest.mark.functionality
@pytest.mark.parametrize(
    "heard, expected",
    [
        ([("EXISTS", [b"11"])], (True, 11)),
        ([("EXPUNGE", [b"4"]), ("EXISTS", [b"10"])], (True, 10)),  # one deleted, then one arrived: same count
        ([("EXPUNGE", [b"4"]), ("FETCH", [b"3 (FLAGS (\\Seen))"])], (False, 9)),
        ([], (False, 10)),
    ],
)
def test_an_email_is_seen_the_moment_it_arrives(heard, expected):
    assert watcher.wait_for_mail(Idling(heard), 10, 60) == expected
