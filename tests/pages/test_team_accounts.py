"""Accounts made by an admin: a new one has no password until its person sets one through its one-time link, the
link works once and dies when replaced, and the last admin can be neither made a member nor closed. Against this
run's own orchestrator schema in the test database."""

import asyncio

import pytest
from psycopg.conninfo import make_conninfo

from app import accounts
from engine import runs
from tarik.core import cabinets
from tests import testdb

SCHEMA = testdb.schema("orchestrator")


def against_the_test_store(check, monkeypatch):
    """``check`` (an async function) run with the run store in this run's own schema, emptied before."""
    testdb.drop(SCHEMA)
    with testdb.connect() as db:
        db.execute(f"create schema {SCHEMA}")
    monkeypatch.setattr(runs, "SCHEMA", SCHEMA)

    async def run():
        await runs.open_store(make_conninfo(**{**cabinets.POSTGRES, "dbname": testdb.DATABASE}))
        try:
            await check()
        finally:
            await runs.pool.close()

    try:
        asyncio.run(run())
    finally:
        testdb.drop(SCHEMA)


@pytest.mark.security
def test_an_invited_account_sets_its_password_once_through_its_link(monkeypatch):
    async def check():
        token = await accounts.invite("New@Cabinet.ma", "Nouvelle", "member")
        assert await accounts.log_in("new@cabinet.ma", "") is None  # no password yet: no way in
        assert (await accounts.invited(token))["email"] == "new@cabinet.ma"
        with pytest.raises(ValueError, match="trop court"):
            await accounts.accept(token, "short")
        await accounts.accept(token, "a long enough password")
        assert await accounts.log_in("new@cabinet.ma", "a long enough password")
        with pytest.raises(ValueError, match="déjà servi"):
            await accounts.accept(token, "another long password")
        assert await accounts.invited(token) is None

    against_the_test_store(check, monkeypatch)


@pytest.mark.security
def test_a_new_link_replaces_the_old_one_and_closing_kills_it(monkeypatch):
    async def check():
        first = await accounts.invite("a@cabinet.ma", "A", "member")
        second = await accounts.new_link("a@cabinet.ma")
        assert await accounts.invited(first) is None and await accounts.invited(second)
        with pytest.raises(ValueError, match="existe déjà"):
            await accounts.invite("a@cabinet.ma", "A", "member")
        await accounts.invite("admin@cabinet.ma", "Admin", "admin")
        await accounts.disable("a@cabinet.ma")
        assert await accounts.invited(second) is None

    against_the_test_store(check, monkeypatch)


@pytest.mark.security
def test_the_last_admin_is_never_demoted_nor_closed(monkeypatch):
    async def check():
        await accounts.invite("admin@cabinet.ma", "Admin", "admin")
        with pytest.raises(ValueError, match="dernier administrateur"):
            await accounts.set_role("admin@cabinet.ma", "member")
        with pytest.raises(ValueError, match="dernier administrateur"):
            await accounts.disable("admin@cabinet.ma")
        await accounts.invite("other@cabinet.ma", "Other", "admin")
        await accounts.set_role("admin@cabinet.ma", "member")  # another admin left: fine
        assert {a["email"]: a["role"] for a in await accounts.listed()}["admin@cabinet.ma"] == "member"

    against_the_test_store(check, monkeypatch)


def asked(handler, method="GET", body=None, path_params=None, query=b"", origin="http://tarik"):
    """``handler``'s answer to admin a@experio.ma's ``method`` (``body`` posted as JSON): (status, the JSON)."""
    import json

    from starlette.requests import Request

    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw}

    scope = {
        "type": "http",
        "method": method,
        "path": "/ui/api/comptes",
        "headers": [(b"host", b"tarik"), (b"origin", origin.encode())],
        "query_string": query,
        "scheme": "http",
        "server": ("tarik", 80),
        "path_params": path_params or {},
    }
    request = Request(scope, receive)
    request.state.user = {"email": "a@experio.ma", "name": "A", "role": "admin"}
    answer = asyncio.run(handler(request))
    return answer.status_code, json.loads(answer.body)


LISTED = [
    {"email": "a@experio.ma", "name": "A", "role": "admin", "active": True, "has_password": True},
    {"email": "b@experio.ma", "name": "B", "role": "member", "active": True, "has_password": False},
    {"email": "c@experio.ma", "name": "C", "role": "member", "active": False, "has_password": True},
]


@pytest.fixture
def comptes(monkeypatch):
    """The Comptes page's handler over accounts in memory: an invitation's token is "abc"; the audit kept aside."""
    from app import pages

    recorded = []

    async def listed():
        return LISTED

    async def invite(email, name, role):
        if "@" not in email:
            raise ValueError("ce n'est pas une adresse email")
        return "abc"

    async def record(*args, **kwargs):
        recorded.append(args)

    async def set_role(email, role):
        raise ValueError("c'est le dernier administrateur : nommez-en un autre d'abord")

    monkeypatch.setattr(accounts, "listed", listed)
    monkeypatch.setattr(accounts, "invite", invite)
    monkeypatch.setattr(accounts, "set_role", set_role)
    monkeypatch.setattr(pages.accounts.audit, "record", record)
    return pages.accounts.accounts_page, recorded


def test_the_comptes_page_lists_each_account_and_shows_a_new_link_once(comptes):
    handler, recorded = comptes
    status, data = asked(handler)
    assert status == 200 and data["accounts"] == LISTED and data["me"] == "a@experio.ma" and data["link"] is None
    status, data = asked(handler, "POST", {"action": "add", "email": " B@Experio.ma", "name": "B", "role": "member"})
    assert status == 200 and data["link"] == "http://tarik/invitation/abc"
    assert data["done"] == "Compte créé pour b@experio.ma." and recorded[0][1] == "account_invited"
    assert asked(handler)[1]["link"] is None  # the page read again: the link is gone


@pytest.mark.security
def test_a_refused_action_is_answered_with_its_reason(comptes):
    handler, recorded = comptes
    status, data = asked(handler, "POST", {"action": "role", "email": "a@experio.ma", "role": "member"})
    assert status == 400 and "dernier administrateur" in data["error"] and recorded == []
    status, data = asked(handler, "POST", {"action": "drop", "email": "a@experio.ma"})
    assert status == 400 and data["error"] == "action inconnue"
    status, data = asked(handler, "POST", {"action": "add", "email": ["x"], "name": "X"})
    assert status == 400 and data["link"] is None  # a field not text is not taken


@pytest.mark.security
def test_an_invitation_s_page_takes_the_password_twice_from_its_own_site_only(monkeypatch):
    from app import pages

    accepted = []

    async def invited(token):
        return {"email": "b@experio.ma", "name": "B"} if token == "abc" else None

    async def accept(token, password):
        accepted.append(password)
        return "b@experio.ma"

    async def record(*args, **kwargs):
        return None

    monkeypatch.setattr(accounts, "invited", invited)
    monkeypatch.setattr(accounts, "accept", accept)
    monkeypatch.setattr(pages.accounts.audit, "record", record)
    handler = pages.accounts.invitation_page
    assert asked(handler, path_params={"token": "old"}) == (404, {"gone": True})
    status, data = asked(handler, path_params={"token": "abc"})
    assert status == 200 and data["found"] == {"email": "b@experio.ma", "name": "B"} and data["min"] == 12
    body = {"password": "a long enough password", "again": "another"}
    status, data = asked(handler, "POST", body, {"token": "abc"})
    assert status == 400 and data["error"] == "les deux mots de passe diffèrent" and accepted == []
    body["again"] = body["password"]
    assert asked(handler, "POST", body, {"token": "abc"}, origin="http://elsewhere")[0] == 403 and accepted == []
    assert asked(handler, "POST", body, {"token": "abc"}) == (200, {"next": "/login?ready=1"})
    assert accepted == ["a long enough password"]


@pytest.mark.security
def test_the_journal_keeps_who_did_what_latest_first_and_filters_it(monkeypatch):
    from app import audit

    async def check():
        await audit.record("a@experio.ma", "login")
        await audit.record("a@experio.ma", "account_role", "b@experio.ma", role="admin")
        await audit.record("b@experio.ma", "layout_saved", "traitement")
        entries, actors = await audit.listed()
        assert [e["action"] for e in entries] == ["layout_saved", "account_role", "login"]
        assert actors == ["a@experio.ma", "b@experio.ma"] and entries[1]["detail"] == {"role": "admin"}
        assert [e["action"] for e in (await audit.listed(actor="a@experio.ma"))[0]] == ["account_role", "login"]
        assert [e["target"] for e in (await audit.listed(action="layout_saved"))[0]] == ["traitement"]
        assert [e["action"] for e in (await audit.listed(before=entries[1]["id"]))[0]] == ["login"]

    against_the_test_store(check, monkeypatch)


def test_the_journal_page_sends_each_entry_with_its_time_and_the_filters(monkeypatch):
    from datetime import UTC, datetime

    from app import audit, pages

    at = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
    entries = [
        {
            "id": 2,
            "at": at,
            "actor": "a@experio.ma",
            "action": "settings_saved",
            "target": "experio_cabinet_8",
            "detail": {"keys": ["DRY_RUN"], "note": "passage en réel"},
        },
        {"id": 1, "at": at, "actor": "x@ailleurs.ma", "action": "login_failed", "target": "", "detail": {}},
    ]
    asked_for = []

    async def listed(action, actor, before, limit):
        asked_for.append((action, actor, before, limit))
        return entries, ["a@experio.ma", "x@ailleurs.ma"]

    monkeypatch.setattr(audit, "listed", listed)
    status, data = asked(pages.journal.journal_page, query=b"action=settings_saved&actor=a%40experio.ma&before=9")
    assert status == 200 and asked_for == [("settings_saved", "a@experio.ma", 9, 100)]
    assert data["entries"][0]["at"].startswith("20/10 ") and data["entries"][0]["detail"]["note"] == "passage en réel"
    assert data["action"] == "settings_saved" and data["actor"] == "a@experio.ma" and data["older"] is None
    assert ["settings_saved", audit_words("settings_saved")] in data["actions"]
    asked(pages.journal.journal_page, query=b"action=nope&before=x")
    assert asked_for[-1] == (None, None, None, 100)  # an unknown action or a page not a number: not taken


def audit_words(action):
    from engine.content import LABELS

    return LABELS.get("audit", {}).get(action, action)
