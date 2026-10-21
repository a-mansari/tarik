"""Who may do what: a member reads every page and talks to Tarik; changing what everyone's Tarik uses (content,
cabinets' settings, a canvas's layout) or starting a run by hand is an admin's, refused to a member by the server
itself, whatever the page shows."""

import asyncio

import pytest

from app import server


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/ui/settings"),
        ("POST", "/ui/tarik-prompts/agent.json"),
        ("POST", "/ui/tarik-prompts-import"),
        ("PUT", "/graphs/traitement/layout"),
        ("DELETE", "/graphs/traitement/layout"),
        ("POST", "/runs/traitement"),
        ("GET", "/ui/comptes"),
        ("GET", "/ui/logs-serveur"),
    ],
)
def test_what_changes_everyone_s_tarik_is_an_admin_s(method, path):
    assert server.admin_only(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/ui/executions"),
        ("GET", "/ui/tarik-prompts/agent.json"),
        ("GET", "/ui/tarik-prompts-export"),
        ("GET", "/graphs/traitement/layout"),
        ("GET", "/ui/discussion/chat-" + "0" * 32),
        ("POST", "/ui/discussion/chat-" + "0" * 32),
        ("POST", "/logout"),
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


def test_a_member_s_change_is_refused_before_it_reaches_the_page(monkeypatch):
    assert called("member", "PUT", "/graphs/traitement/layout", monkeypatch) == (403, False)
    assert called("member", "POST", "/runs/traitement", monkeypatch) == (403, False)


def test_an_admin_s_change_and_a_member_s_reading_go_through(monkeypatch):
    assert called("admin", "PUT", "/graphs/traitement/layout", monkeypatch) == (200, True)
    assert called("member", "GET", "/graphs/traitement/layout", monkeypatch) == (200, True)
