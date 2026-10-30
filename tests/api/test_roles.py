"""Who may do what: a member reads every page and talks to Tarik; changing what everyone's Tarik uses (content,
cabinets' settings, a canvas's layout) or starting a run by hand is an admin's, refused to a member by the server
itself, whatever the page shows."""

import asyncio

import pytest

from server import server


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/ui/api/settings"),
        ("GET", "/ui/api/routines"),
        ("POST", "/ui/api/routines"),
        ("POST", "/ui/api/tarik-prompts/agent.json"),
        ("POST", "/ui/api/tarik-prompts-import"),
        ("PUT", "/graphs/traitement/layout"),
        ("DELETE", "/graphs/traitement/layout"),
        ("POST", "/runs/traitement"),
        ("POST", "/ui/api/executions/r1/stop"),
        ("GET", "/ui/api/comptes"),
        ("GET", "/ui/api/logs-serveur"),
    ],
)
def test_what_changes_everyone_s_tarik_is_an_admin_s(method, path):
    assert server.admin_only(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/ui/api/executions"),
        ("GET", "/ui/api/tarik-prompts/agent.json"),
        ("GET", "/ui/api/tarik-prompts-export"),
        ("GET", "/graphs/traitement/layout"),
        ("GET", "/ui/api/discussion/chat-" + "0" * 32),
        ("POST", "/ui/api/discussion/chat-" + "0" * 32),
        ("POST", "/ui/api/logout"),
    ],
)
def test_reading_and_talking_to_tarik_are_every_member_s(method, path):
    assert not server.admin_only(method, path)


def called(role, method, path, monkeypatch):
    """The status the Guard answers a session of ``role`` with, and whether the call reached the page."""

    async def user_of(token):
        return {"email": "a@experio.ma", "name": "A", "role": role}

    monkeypatch.setattr(server.accounts, "user_of", user_of)
    reached, sent = [], []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    headers = [(b"cookie", b"tarik_session=t"), (b"host", b"tarik"), (b"origin", b"http://tarik")]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("tarik", 80),
    }
    asyncio.run(server.Guard(app)(scope, receive, send))
    return (sent[0]["status"] if sent else 200), bool(reached)


def test_a_routine_is_never_started_by_hand_even_by_an_admin():
    import json

    async def receive():
        return {"type": "http.request", "body": json.dumps({"input": {}}).encode()}

    from starlette.requests import Request

    scope = {"type": "http", "method": "POST", "path": "/runs/routine", "headers": []}
    scope["path_params"] = {"graph": "routine"}
    answer = asyncio.run(server.start(Request(scope, receive)))
    assert answer.status_code == 403  # its period is claimed by its schedule first


def test_a_member_s_change_is_refused_before_it_reaches_the_page(monkeypatch):
    assert called("member", "PUT", "/graphs/traitement/layout", monkeypatch) == (403, False)
    assert called("member", "POST", "/runs/traitement", monkeypatch) == (403, False)


def test_a_member_never_reads_an_admin_s_page_s_data(monkeypatch):
    assert called("member", "GET", "/ui/api/settings", monkeypatch) == (403, False)
    assert called("member", "GET", "/ui/settings", monkeypatch) == (200, True)  # the app alone: no data in it


def test_an_admin_s_change_and_a_member_s_reading_go_through(monkeypatch):
    assert called("admin", "PUT", "/graphs/traitement/layout", monkeypatch) == (200, True)
    assert called("member", "GET", "/graphs/traitement/layout", monkeypatch) == (200, True)


def no_account(monkeypatch, failures=None):
    """log_in's database holding no account, each query yielding once so guesses sent at once interleave."""
    from server import accounts

    class Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *args):
            await asyncio.sleep(0)
            return self

        async def fetchone(self):
            return None

    monkeypatch.setattr(accounts.runs, "pool", type("Pool", (), {"connection": lambda self: Db()})())
    monkeypatch.setattr(accounts, "FAILURES", failures or {})
    monkeypatch.setattr(accounts, "HASHING", asyncio.Semaphore(2))  # a semaphore keeps the loop it first waited on
    return accounts


def logged_in(monkeypatch, body, address="1.2.3.4"):
    """server.login's answer to ``body`` (bytes) from this server's own page, in a browser at ``address``."""
    from starlette.requests import Request

    from server import audit

    async def receive():
        return {"type": "http.request", "body": body}

    async def recorded(*args):
        return None

    monkeypatch.setattr(audit, "record", recorded)
    headers = [(b"host", b"tarik"), (b"origin", b"http://tarik")]
    scope = {"type": "http", "method": "POST", "path": "/ui/api/login", "headers": headers, "query_string": b""}
    scope["client"] = (address, 443)  # as uvicorn sets it from Funnel's X-Forwarded-For
    return asyncio.run(server.login(Request(scope, receive)))


@pytest.mark.security
def test_the_login_throttle_forgets_what_its_wait_is_over(monkeypatch):
    from server import accounts

    no_account(monkeypatch, {("old@x", "B"): (3, accounts.time.monotonic() - accounts.WAIT_SECONDS - 1)})
    asyncio.run(accounts.log_in("new@x", "nope", "A"))
    assert set(accounts.FAILURES) == {("new@x", "A")}  # old tries of any email or address dropped: never grows


@pytest.mark.security
def test_the_try_that_reaches_the_limit_is_told_to_wait_not_called_wrong(monkeypatch):
    from server import accounts

    no_account(monkeypatch, {("a@x", "1.2.3.4"): (accounts.WAIT_AFTER - 1, accounts.time.monotonic())})
    assert logged_in(monkeypatch, b'{"email": "a@x", "password": "nope"}', "1.2.3.4").status_code == 429


@pytest.mark.security
def test_an_email_or_password_too_long_to_be_real_is_never_hashed(monkeypatch):
    from server import accounts

    async def never(*args):
        raise AssertionError("never hashed nor counted")

    monkeypatch.setattr(accounts, "log_in", never)
    assert logged_in(monkeypatch, b'{"email": "a@x", "password": "%s"}' % (b"x" * 1025)).status_code == 400
    assert logged_in(monkeypatch, b'{"email": "%s@x", "password": "p"}' % (b"a" * 260)).status_code == 400


@pytest.mark.security
def test_guesses_sent_at_once_each_count_and_an_ended_wait_starts_again(monkeypatch):
    accounts = no_account(monkeypatch)

    async def guesses(count):
        await asyncio.gather(*(accounts.log_in("a@x", "nope", "A") for _ in range(count)))

    asyncio.run(guesses(accounts.WAIT_AFTER))
    assert accounts.waiting("a@x", "A")  # not one count for the whole burst
    over = accounts.time.monotonic() - accounts.WAIT_SECONDS - 1
    no_account(monkeypatch, {("a@x", "A"): (accounts.WAIT_AFTER, over)})
    asyncio.run(guesses(1))
    assert not accounts.waiting("a@x", "A")  # one more mistake after the wait is the first of 5 again


@pytest.mark.security
def test_an_unknown_account_is_hashed_like_a_real_one_so_its_answer_is_as_slow(monkeypatch):
    accounts = no_account(monkeypatch)
    checked = []
    monkeypatch.setattr(accounts, "matches", lambda password, stored: checked.append(stored) or True)
    assert asyncio.run(accounts.log_in("nobody@x", "anything", "A")) is None  # refused even when the dummy "matches"
    assert checked == [accounts.UNSET]


@pytest.mark.security
def test_a_guessing_run_from_one_address_never_locks_the_person_out_from_theirs(monkeypatch):
    from server import accounts

    no_account(monkeypatch, {("boss@x", "6.6.6.6"): (accounts.WAIT_AFTER, accounts.time.monotonic())})
    body = b'{"email": "boss@x", "password": "nope"}'
    assert logged_in(monkeypatch, body, "6.6.6.6").status_code == 429  # the guesser waits
    assert logged_in(monkeypatch, body, "105.72.1.1").status_code == 401  # the boss, elsewhere, is still checked
