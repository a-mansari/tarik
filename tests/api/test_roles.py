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


@pytest.mark.security
def test_the_login_throttle_forgets_what_its_wait_is_over(monkeypatch):
    import asyncio

    from server import accounts

    class Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *args):
            return self

        async def fetchone(self):
            return None  # no such account

    monkeypatch.setattr(accounts.runs, "pool", type("Pool", (), {"connection": lambda self: Db()})())
    monkeypatch.setattr(accounts, "FAILURES", {"old@x": (3, accounts.time.monotonic() - accounts.WAIT_SECONDS - 1)})
    asyncio.run(accounts.log_in("new@x", "nope"))
    assert set(accounts.FAILURES) == {"new@x"}  # a guess with a new address never grows it without end
