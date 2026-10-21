"""A cabinet's settings changed from the Cabinets page: what is refused, what a form changes, and what is written to
Vault and recorded (never a secret's value, never over a newer version, any other key in the path kept)."""

import asyncio
import contextlib

import pytest

from app import cabinet_settings

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


def test_a_page_older_than_vault_s_version_changes_nothing(vault):
    with pytest.raises(ValueError, match="rechargez"):
        asyncio.run(cabinet_settings.save(Pool(), "cabinet_a", {"DRY_RUN": "0"}, 3, "a@experio.ma"))
    assert vault == []


def test_the_page_renders_a_saved_change_and_never_a_secret():
    from ui import settings_pages

    data = {**BEFORE, "IMAP_PASSWORD": "mailbox-secret"}
    page = settings_pages.templates.get_template("cabinet.html").render(
        request=type(
            "R", (), {"cookies": {}, "url": type("U", (), {"path": "/ui/cabinets/cabinet_a"})(), "state": None}
        )(),
        cabinet="cabinet_a",
        sections=settings_pages.fields(data),
        version=4,
        saved=["MAIL_ALLOWED_RECIPIENTS", "DRY_RUN"],
        names={key: key for key in cabinet_settings.FIELDS},
        history=[],
    )
    assert "Enregistré : MAIL_ALLOWED_RECIPIENTS, DRY_RUN." in page
    assert "old-password" not in page and "mailbox-secret" not in page
