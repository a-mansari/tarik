"""A cabinet's settings changed from the Cabinets page: what is refused, what a form changes, and what is written to
Vault and recorded (never a secret's value, never over a newer version, any other key in the path kept)."""

import asyncio
import contextlib

import pytest

from server import cabinet_settings

BEFORE = {
    "DRY_RUN": "1",
    "ODOO_USER": "tarik@cabinet.ma",
    "ODOO_UID": "12",
    "ODOO_PASSWORD": "old-password",
    "MAIL_ALLOWED_RECIPIENTS": "@cabinet.ma",
    "OTHER_KEY": "kept",  # a key the page does not show, in the same path
}


@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        ("DRY_RUN", "oui", "vaut 1"),
        ("ODOO_UID", "-3", "entier positif"),
        ("ODOO_USER", "", "vide"),
        ("MAIL_MANAGER", "pas une adresse", "adresse"),
        ("MODEL_ADAPTER", "gpt", "l'un de"),
        ("SMTP_FROM", "Tarik <pas-une-adresse>", "expéditeur"),
        ("MAIL_ALLOWED_RECIPIENTS", "@cabinet.ma, n'importe qui", "n'importe qui"),
    ],
)
def test_a_setting_tarik_could_not_use_is_refused(key, value, why):
    with pytest.raises(ValueError, match=why):
        cabinet_settings.checked(key, value)


def test_the_recipients_take_what_tarik_reads():
    cabinet_settings.checked("MAIL_ALLOWED_RECIPIENTS", "test.*@experio.ma, @sarnec.com, a@b.ma, ")
    cabinet_settings.checked("MAIL_ALLOWED_RECIPIENTS", "*")


def test_a_form_changes_only_what_differs_and_a_secret_left_empty_stays():
    given = {
        "DRY_RUN": "1",
        "ODOO_USER": "tarik@cabinet.ma",
        "ODOO_PASSWORD": "",
        "MAIL_ALLOWED_RECIPIENTS": "@cabinet.ma ,  a@b.ma",
        "MAIL_MANAGER": "",
    }
    assert cabinet_settings.changed(BEFORE, given) == {"MAIL_ALLOWED_RECIPIENTS": "@cabinet.ma ,  a@b.ma"}


def test_a_setting_loses_its_outer_spaces_only_and_a_secret_none():
    before = {"MAIL_ALLOWED_RECIPIENTS": "@experio.ma,@sarnec.com", "SMTP_PASSWORD": "x"}
    assert cabinet_settings.changed(before, {"MAIL_ALLOWED_RECIPIENTS": " @experio.ma,@sarnec.com "}) == {}
    spaced = {"MAIL_ALLOWED_RECIPIENTS": "@experio.ma, @sarnec.com", "SMTP_PASSWORD": " pass word "}
    assert cabinet_settings.changed(before, spaced) == spaced  # inside a list, and a password's spaces, are kept


@pytest.mark.parametrize(("key", "value"), [("ODOO_UID", "4 30"), ("MAIL_MANAGER", "a b@c.ma"), ("ODOO_USER", "  ")])
def test_a_value_tarik_would_read_otherwise_is_refused_not_tidied(key, value):
    with pytest.raises(ValueError):
        cabinet_settings.changed({}, {key: value})


class Pool:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def connection(self):
        pool = self

        class Db:
            async def execute(self, statement, values):
                pool.recorded.append(values)

        yield Db()


@pytest.fixture
def vault(monkeypatch):
    written = []

    def answer(method, path, **kwargs):
        if method == "GET":
            return {"data": {"data": dict(BEFORE), "metadata": {"version": 4}}}
        written.append(kwargs["json"])
        return {"data": {"version": 5}}

    monkeypatch.setattr(cabinet_settings, "_vault", answer)
    monkeypatch.setattr(cabinet_settings.cabinets, "CABINETS", frozenset({"cabinet_a"}))
    monkeypatch.setattr(cabinet_settings.cabinets, "reread_vault", lambda: None)
    return written


def test_a_change_is_written_over_the_version_shown_and_recorded_without_any_secret(vault):
    pool = Pool()
    given = {"DRY_RUN": "0", "ODOO_PASSWORD": "new-password"}
    done = asyncio.run(cabinet_settings.save(pool, "cabinet_a", given, 4, "a@experio.ma", "passage en réel"))
    assert sorted(done) == ["DRY_RUN", "ODOO_PASSWORD"]
    assert vault == [{"options": {"cas": 4}, "data": {**BEFORE, "DRY_RUN": "0", "ODOO_PASSWORD": "new-password"}}]
    ((cabinet, author, note, changes, version),) = pool.recorded
    assert (cabinet, author, note, version) == ("cabinet_a", "a@experio.ma", "passage en réel", 5)
    assert {"key": "ODOO_PASSWORD", "before": None, "after": None} in changes.obj
    assert {"key": "DRY_RUN", "before": "1", "after": "0"} in changes.obj
    assert "password" not in repr(pool.recorded)


def test_the_model_is_kept_in_postgres_never_in_vault(vault):
    pool = Pool()
    done = asyncio.run(cabinet_settings.save(pool, "cabinet_a", {"MODEL_ADAPTER": "claude-code"}, 4, "a@experio.ma"))
    assert done == ["MODEL_ADAPTER"] and vault == []
    (chosen, (cabinet, _, _, changes, version)) = pool.recorded
    assert chosen == ("cabinet_a", "claude-code") and version == 4  # Vault's version, as it was
    assert changes.obj == [{"key": "MODEL_ADAPTER", "before": "", "after": "claude-code"}]


def test_a_page_older_than_vault_s_version_changes_nothing(vault):
    with pytest.raises(ValueError, match="rechargez"):
        asyncio.run(cabinet_settings.save(Pool(), "cabinet_a", {"DRY_RUN": "0"}, 3, "a@experio.ma"))
    assert vault == []


def asked(handler, method="GET", body=None, path_params=None):
    """``handler``'s answer to an admin's ``method`` (``body`` posted as JSON): (status, the JSON, its raw text)."""
    import json

    from starlette.requests import Request

    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw}

    scope = {"type": "http", "method": method, "path": "/ui/api/settings", "headers": [], "query_string": b""}
    scope["path_params"] = path_params or {}
    request = Request(scope, receive)
    request.state.user = {"email": "a@experio.ma", "name": "A", "role": "admin"}
    answer = asyncio.run(handler(request))
    return answer.status_code, json.loads(answer.body), answer.body.decode()


@pytest.fixture
def settings_page(vault, monkeypatch):
    """The Réglages page's handler over the fake Vault, its cabinet cabinet_a, a secret's change in its history."""
    from server import api

    monkeypatch.setattr(api.settings, "chosen", lambda request: "cabinet_a")
    monkeypatch.setattr(cabinet_settings.model, "chosen_adapter", lambda cabinet: "minimax")
    vault_data = {**BEFORE, "IMAP_PASSWORD": "mailbox-secret"}
    monkeypatch.setattr(
        cabinet_settings,
        "_vault",
        lambda method, path, **kw: (
            {"data": {"data": dict(vault_data), "metadata": {"version": 4}}}
            if method == "GET"
            else vault.append(kw["json"]) or {"data": {"version": 5}}
        ),
    )

    async def history(pool, cabinet):
        changes = [{"key": "ODOO_PASSWORD", "before": None, "after": None}]
        return [
            {"author": "a@experio.ma", "note": "", "changes": changes, "version": 4, "at": "2026-10-20T18:00:00+00:00"}
        ]

    monkeypatch.setattr(cabinet_settings, "history", history)
    monkeypatch.setattr(api.settings.runs, "pool", Pool())
    return api.settings.cabinet_page


@pytest.mark.security
def test_the_page_s_data_never_holds_a_secret_only_whether_it_is_set(settings_page):
    status, data, raw = asked(settings_page)
    assert status == 200 and data["version"] == 4 and data["dry_run"] is True and data["saved"] is None
    shown = {f["key"]: f["value"] for _, items in data["sections"] for f in items}
    assert shown["ODOO_PASSWORD"] is True and shown["IMAP_PASSWORD"] is True and shown["SMTP_PASSWORD"] is False
    assert shown["ODOO_USER"] == "tarik@cabinet.ma"
    assert "old-password" not in raw and "mailbox-secret" not in raw
    assert data["history"][0]["at"].startswith("20/10 ") and data["history"][0]["changes"][0]["before"] is None


@pytest.mark.security
def test_a_saved_change_is_answered_with_the_page_and_never_a_secret(settings_page, vault):
    given = {"cabinet": "cabinet_a", "version": "4", "MAIL_ALLOWED_RECIPIENTS": "@cabinet.ma, a@b.ma"}
    status, data, raw = asked(settings_page, "POST", {**given, "IMAP_PASSWORD": "typed-secret"})
    assert status == 200 and sorted(data["saved"]) == ["IMAP_PASSWORD", "MAIL_ALLOWED_RECIPIENTS"]
    assert vault[0]["options"] == {"cas": 4}
    assert "typed-secret" not in raw and "mailbox-secret" not in raw and "old-password" not in raw


@pytest.mark.security
def test_a_refused_change_sends_back_what_was_typed_but_never_a_secret(settings_page, vault):
    given = {"cabinet": "cabinet_a", "version": "4", "ODOO_UID": "-3", "SMTP_PASSWORD": "typed-secret"}
    status, data, raw = asked(settings_page, "POST", given)
    assert status == 400 and "entier positif" in data["error"] and vault == []
    shown = {f["key"]: f["value"] for _, items in data["sections"] for f in items}
    assert shown["ODOO_UID"] == "-3" and shown["SMTP_PASSWORD"] is False
    assert "typed-secret" not in raw and "mailbox-secret" not in raw


def test_a_change_for_another_cabinet_or_an_older_page_is_refused(settings_page, vault):
    status, data, _ = asked(settings_page, "POST", {"cabinet": "cabinet_b", "version": "4", "DRY_RUN": "0"})
    assert status == 400 and "un autre cabinet" in data["error"]
    status, data, _ = asked(settings_page, "POST", {"cabinet": "cabinet_a", "version": "3", "DRY_RUN": "0"})
    assert status == 400 and "rechargez" in data["error"] and vault == []
    status, data, _ = asked(settings_page, "POST", {"cabinet": "cabinet_a", "version": 4, "DRY_RUN": "0"})
    assert status == 400 and vault == []  # a field not text (here the version) is not taken
