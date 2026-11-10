"""The settings every cabinet shares, changed from the general settings page: what is refused, and what is written to
each Vault path and recorded (never a secret's value, never over a newer version, any other key in a path kept)."""

import asyncio
import contextlib

import pytest

from server import cabinet_settings, shared_settings

SHARED = {
    "IMAP_HOST": "imap.old.ma",
    "IMAP_PORT": "993",
    "SPARROW_URL": "https://sparrow.old.ma",
    "SPARROW_TOKEN": "t0",
    "OTHER": "kept",
}
ORCHESTRATOR = {"MODEL_API_KEY": "old-key", "EXPERIO_API_KEY": "old-api", "ELSE": "kept too"}


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
            data, version = (ORCHESTRATOR, 7) if path.endswith("orchestrator") else (SHARED, 3)
            return {"data": {"data": dict(data), "metadata": {"version": version}}}
        written.append((path.rsplit("/", 1)[1], kwargs["json"]))
        return {"data": {"version": 9}}

    monkeypatch.setattr(cabinet_settings, "_vault", answer)
    monkeypatch.setattr(shared_settings.cabinets, "reread_vault", lambda: None)

    async def record(*args, **kwargs):
        written.append(("audit", args, kwargs))

    monkeypatch.setattr(shared_settings.audit, "record", record)
    return written


SEEN = "7.3"  # the page's version text: orchestrator 7, shared 3


def save(pool, given, seen=SEEN):
    return asyncio.run(shared_settings.save(pool, given, seen, "a@experio.ma", "test"))


def test_every_shared_setting_has_its_place_and_its_path():
    assert set(shared_settings.FIELDS) == shared_settings.cabinets.SHARED_SETTINGS
    assert {shared_settings.path_of(key) for key in shared_settings.FIELDS} == {"orchestrator", "shared"}


@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        ("IMAP_PORT", "99 3", "entier positif"),
        ("IMAP_HOST", "", "vide"),
        ("PUBLIC_URL", "http://tarik.ma", "HTTPS"),
        ("GLADDEN_URL", "https://user@evil.example", "HTTPS"),
        ("MAIL_CERT_SHA256", "abc", "64 caractères"),
    ],
)
def test_a_setting_tarik_could_not_use_is_refused(key, value, why):
    with pytest.raises(ValueError, match=why):
        cabinet_settings.checked(key, value, shared_settings.FIELDS, shared_settings.OPTIONAL)


def test_a_change_goes_to_its_own_path_over_that_path_s_version_and_keeps_the_other_keys(vault):
    pool = Pool()
    done = save(pool, {"IMAP_HOST": "imap.new.ma", "MODEL_API_KEY": "new-key"})
    assert sorted(done) == ["IMAP_HOST", "MODEL_API_KEY"]
    assert ("shared", {"options": {"cas": 3}, "data": {**SHARED, "IMAP_HOST": "imap.new.ma"}}) in vault
    assert ("orchestrator", {"options": {"cas": 7}, "data": {**ORCHESTRATOR, "MODEL_API_KEY": "new-key"}}) in vault
    assert not any(
        isinstance(one[0], str) and one[0] == "orchestrator" and "IMAP_HOST" in one[1]["data"] for one in vault
    )


@pytest.mark.security
def test_no_secret_is_recorded_not_even_the_shared_ones(vault):
    pool = Pool()
    save(pool, {"MODEL_API_KEY": "new-key", "SPARROW_TOKEN": "new-token", "IMAP_PORT": "143"})
    ((cabinet, author, note, changes, version),) = pool.recorded
    assert (cabinet, author, note, version) == (shared_settings.GENERAL, "a@experio.ma", "test", 9)
    assert {"key": "MODEL_API_KEY", "before": None, "after": None} in changes.obj
    assert {"key": "SPARROW_TOKEN", "before": None, "after": None} in changes.obj
    assert {"key": "IMAP_PORT", "before": "993", "after": "143"} in changes.obj
    assert "key" not in repr(pool.recorded).replace("'key'", "").replace('"key"', "")  # no value of a key
    assert "new-token" not in repr(pool.recorded) and "old-key" not in repr(pool.recorded)


def test_a_secret_left_empty_stays_and_nothing_changed_writes_nothing(vault):
    assert save(Pool(), {"MODEL_API_KEY": "", "IMAP_HOST": "imap.old.ma"}) == []
    assert vault == []


def test_a_page_older_than_vault_is_refused(vault):
    with pytest.raises(ValueError, match="ont changé"):
        save(Pool(), {"IMAP_PORT": "143"}, seen="6.3")
    assert vault == []


@pytest.mark.security
def test_a_new_address_needs_its_secret_typed_again(vault):
    """Tarik sends the token to SPARROW_URL: a new address never gets the stored one."""
    with pytest.raises(ValueError, match="retapez aussi SPARROW_TOKEN"):
        save(Pool(), {"SPARROW_URL": "https://other.example"})
    assert vault == []
    assert save(Pool(), {"SPARROW_URL": "https://other.example", "SPARROW_TOKEN": "t1"})


def test_a_secret_is_only_shown_as_set_or_not():
    shown = cabinet_settings.shown({**SHARED, **ORCHESTRATOR}, shared_settings.FIELDS)
    assert shown["MODEL_API_KEY"] is True and shown["CLAUDE_CODE_OAUTH_TOKEN"] is False
    assert shown["IMAP_HOST"] == "imap.old.ma" and "old-key" not in repr(shown)


@pytest.mark.parametrize("url", ["https://good.example\\.evil.example", "https://user@evil.example", "http://x.ma"])
def test_an_address_with_a_backslash_a_login_or_no_https_is_refused(url):
    with pytest.raises(ValueError, match="HTTPS"):
        cabinet_settings.checked("PUBLIC_URL", url, shared_settings.FIELDS, shared_settings.OPTIONAL)


def test_a_stored_value_tarik_accepts_blocks_no_other_change():
    before = {"MODEL_API_URL": "http://proxy.internal:4000", "IMAP_FOLDER": "", "IMAP_HOST": "imap.old.ma"}
    given = {"MODEL_API_URL": "http://proxy.internal:4000", "IMAP_HOST": "imap.new.ma"}
    assert cabinet_settings.changed(before, given, shared_settings.FIELDS, shared_settings.OPTIONAL) == {
        "IMAP_HOST": "imap.new.ma"
    }
    with pytest.raises(ValueError, match="HTTPS"):  # changed, it is checked
        cabinet_settings.changed(
            before, {"MODEL_API_URL": "http://other"}, shared_settings.FIELDS, shared_settings.OPTIONAL
        )


def failing_second_write(monkeypatch, status):
    written = []

    def answer(method, path, **kwargs):
        if method == "GET":
            data, version = (ORCHESTRATOR, 7) if path.endswith("orchestrator") else (SHARED, 3)
            return {"data": {"data": dict(data), "metadata": {"version": version}}}
        if path.endswith("shared"):
            raise shared_settings.httpx.HTTPStatusError(
                "x",
                request=shared_settings.httpx.Request("POST", "http://v"),
                response=shared_settings.httpx.Response(status),
            )
        written.append(kwargs["json"])
        return {"data": {"version": 9}}

    monkeypatch.setattr(cabinet_settings, "_vault", answer)
    monkeypatch.setattr(shared_settings.cabinets, "reread_vault", lambda: None)
    monkeypatch.setattr(shared_settings.audit, "record", lambda *args, **kwargs: asyncio.sleep(0))
    return written


@pytest.mark.parametrize(("status", "words"), [(400, "ont changé"), (403, "refusé l'écriture \\(403\\)")])
def test_when_the_second_path_fails_the_first_is_recorded_and_the_error_says_so(monkeypatch, status, words):
    written, pool = failing_second_write(monkeypatch, status), Pool()
    with pytest.raises(ValueError, match=words) as refused:
        save(pool, {"MODEL_API_KEY": "new-key", "IMAP_PORT": "143"})
    assert "déjà enregistrés : MODEL_API_KEY" in str(refused.value)
    assert len(written) == 1 and len(pool.recorded) == 1  # the key is in Vault, so it is in the history too
    assert [change["key"] for change in pool.recorded[0][3].obj] == ["MODEL_API_KEY"]
