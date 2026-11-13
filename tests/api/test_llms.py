"""The extra global LLMs changed from the general settings page: what is refused, and what is written to Vault and
recorded (never a key, never over a newer version, a removed one's cabinets told)."""

import asyncio
import contextlib

import pytest

from server import cabinet_settings, llms

BEFORE = {"opus": {"type": "anthropic-api", "model": "claude-opus", "url": "https://api.example", "key": "k-opus"}}
NEW = {"name": "haiku", "type": "claude-code", "model": "haiku", "url": "", "key": "token"}


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
            return {"data": {"data": dict(BEFORE), "metadata": {"version": 3}}}
        written.append(kwargs["json"])
        return {"data": {"version": 4}}

    monkeypatch.setattr(cabinet_settings, "_vault", answer)
    monkeypatch.setattr(llms.cabinets, "reread_vault", lambda: None)
    monkeypatch.setattr(
        llms.cabinets,
        "vault_settings",
        lambda: {"cabinet_a": {"LLM_CHOICE": "opus"}, "cabinet_b": {"LLM_CHOICE": "ghost"}},
    )

    async def record(*args, **kwargs):
        written.append(("audit", args))

    monkeypatch.setattr(llms.audit, "record", record)
    return written


def keep(*more):
    return [
        {"name": "opus", "type": "anthropic-api", "model": "claude-opus", "url": "https://api.example", "key": ""},
        *more,
    ]


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({**NEW, "name": "Claude Two"}, "un nom"),
        ({**NEW, "name": "claude"}, "autre que"),
        ({**NEW, "name": "custom"}, "autre que"),
        ({**NEW, "type": "gpt"}, "le type vaut"),
        ({**NEW, "model": " "}, "modèle"),
        ({**NEW, "key": ""}, "clé"),
        ({**NEW, "type": "anthropic-api", "url": "http://insecure.example"}, "HTTPS"),
    ],
)
def test_an_llm_tarik_could_not_run_is_refused(entry, why):
    with pytest.raises(ValueError, match=why):
        llms.checked(keep(entry), BEFORE)


def test_a_name_is_taken_once():
    with pytest.raises(ValueError, match="déjà pris"):
        llms.checked(keep(NEW, NEW), BEFORE)


def test_a_key_left_empty_stays_and_an_unchanged_form_changes_nothing():
    assert llms.checked(keep(), BEFORE) == BEFORE


@pytest.mark.security
def test_a_new_address_needs_the_key_typed_again():
    moved = [{**keep()[0], "url": "https://other.example"}]
    with pytest.raises(ValueError, match="retapez aussi la clé"):
        llms.checked(moved, BEFORE)
    assert llms.checked([{**moved[0], "key": "k2"}], BEFORE)["opus"]["key"] == "k2"


def test_a_change_is_written_over_the_version_shown_and_recorded_without_any_key(vault):
    pool = Pool()
    done = asyncio.run(llms.save(pool, keep(NEW), 3, "a@experio.ma"))
    assert done == {"saved": ["haiku"], "fell_back": []}
    assert vault[0] == {
        "options": {"cas": 3},
        "data": {**BEFORE, "haiku": {k: NEW[k] for k in ("type", "model", "url", "key")}},
    }
    ((cabinet, author, _, changes, version),) = pool.recorded
    assert (cabinet, author, version) == (llms.LLMS, "a@experio.ma", 4)
    assert "token" not in repr(pool.recorded) and "k-opus" not in repr(pool.recorded)
    assert changes.obj == [{"key": "haiku", "before": "", "after": "claude-code · haiku"}]


def test_removing_an_llm_tells_which_cabinets_fall_back_to_claude(vault):
    done = asyncio.run(llms.save(Pool(), [], 3, "a@experio.ma"))
    assert done == {"saved": ["opus"], "fell_back": ["cabinet_a"]}


@pytest.mark.security
def test_a_removed_llm_s_name_cannot_be_given_to_another_while_a_cabinet_still_names_it(vault):
    with pytest.raises(ValueError, match="un cabinet a encore ce nom"):  # cabinet_b still names "ghost", gone
        asyncio.run(llms.save(Pool(), keep({**NEW, "name": "ghost"}), 3, "a@experio.ma"))
    assert asyncio.run(llms.save(Pool(), keep(NEW), 3, "a@experio.ma"))["saved"] == ["haiku"]  # a free name is fine


def test_a_page_older_than_vault_is_refused_and_a_vault_refusal_is_told(vault, monkeypatch):
    with pytest.raises(ValueError, match="ont changé"):
        asyncio.run(llms.save(Pool(), keep(NEW), 2, "a@experio.ma"))
    assert vault == []


def test_a_vault_that_refuses_is_not_a_page_gone_stale(monkeypatch):
    import httpx

    def refuse(method, path, **kw):
        if method == "GET":
            return {"data": {"data": dict(BEFORE), "metadata": {"version": 3}}}
        raise httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://v"), response=httpx.Response(403))

    monkeypatch.setattr(cabinet_settings, "_vault", refuse)
    monkeypatch.setattr(llms.cabinets, "vault_settings", lambda: {})
    with pytest.raises(ValueError, match=r"refusé l'écriture \(403\)"):
        asyncio.run(llms.save(Pool(), keep(NEW), 3, "a@experio.ma"))


def test_the_page_only_knows_whether_a_key_is_set():
    shown = llms.shown({**BEFORE, "x": {"type": "claude-code", "model": "m"}})
    assert [(one["name"], one["key_set"]) for one in shown] == [("opus", True), ("x", False)]
    assert "k-opus" not in repr(shown)
