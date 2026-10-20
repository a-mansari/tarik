"""Accounts made by an admin: a new one has no password until its person sets one through its one-time link, the
link works once and dies when replaced, and the last admin can be neither made a member nor closed. Against this
run's own orchestrator schema in the test database."""

import asyncio

import pytest
from psycopg.conninfo import make_conninfo

from engine import accounts, runs
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


def test_the_comptes_page_renders_each_account_and_a_new_link_once():
    from ui.pages import templates

    listed = [
        {"email": "a@experio.ma", "name": "A", "role": "admin", "active": True, "has_password": True},
        {"email": "b@experio.ma", "name": "B", "role": "member", "active": True, "has_password": False},
        {"email": "c@experio.ma", "name": "C", "role": "member", "active": False, "has_password": True},
    ]
    user = {"email": "a@experio.ma", "name": "A", "role": "admin"}
    request = type(
        "R",
        (),
        {"cookies": {}, "url": type("U", (), {"path": "/ui/comptes"})(), "state": type("S", (), {"user": user})()},
    )
    page = templates.get_template("accounts.html").render(
        request=request(),
        accounts=listed,
        done="Compte créé pour b@experio.ma.",
        error=None,
        link="http://tarik/invitation/abc",
        hours=72,
        me="a@experio.ma",
    )
    assert "http://tarik/invitation/abc" in page and "Invité, sans mot de passe" in page and "Fermé" in page
    assert "Rendre membre" in page and "Rouvrir avec un lien" in page and "(vous)" in page


@pytest.mark.security
def test_the_journal_keeps_who_did_what_latest_first_and_filters_it(monkeypatch):
    from engine import audit

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


def test_the_journal_page_renders_each_entry_in_words():
    from datetime import UTC, datetime

    from ui.pages import templates

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
    user = {"email": "a@experio.ma", "name": "A", "role": "admin"}
    request = type(
        "R",
        (),
        {"cookies": {}, "url": type("U", (), {"path": "/ui/journal"})(), "state": type("S", (), {"user": user})()},
    )
    page = templates.get_template("journal.html").render(
        request=request(),
        entries=entries,
        actors=["a@experio.ma"],
        actions=[],
        action=None,
        actor="",
        older=None,
        words={"settings_saved": "Réglages modifiés", "login_failed": "Connexion refusée"},
    )
    assert "Réglages modifiés" in page and "DRY_RUN" in page and "« passage en réel »" in page
    assert "Connexion refusée" in page and "x@ailleurs.ma" in page
