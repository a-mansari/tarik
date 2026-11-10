"""Which LLM a cabinet runs on (engine/tools/core/model.py llm): the global one it picks (Claude, MiniMax or an extra),
or its own; Claude when it picks none, or one removed since; never another cabinet's."""

import pytest

from engine.tools.core import cabinets, model

EXTRAS = {"opus": {"type": "anthropic-api", "model": "claude-opus", "url": "https://api.example", "key": "k-opus"}}


@pytest.fixture
def served(monkeypatch):
    """Two cabinets served, settings from the environment, no extras, nothing chosen before the LLMs."""
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"cabinet_a", "cabinet_b"}))
    monkeypatch.setattr(model, "chosen_adapter", lambda cabinet: "")
    monkeypatch.setattr(model, "model_name", lambda: "MiniMax-M3")
    monkeypatch.setattr(model.cabinets, "llm_extras", lambda: {})
    monkeypatch.setenv("MODEL_API_KEY", "mm-key")
    monkeypatch.setenv("MODEL_API_URL", "https://mm.example")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-token")

    def pick(cabinet="cabinet_a", **own):
        for key, value in own.items():
            monkeypatch.setenv(f"{cabinet.upper()}__{key}", value)
        with cabinets.using(cabinet):
            return model.llm()

    return pick


def test_nothing_picked_is_claude_with_the_global_token(served):
    found = served()
    assert (found.name, found.type, found.model, found.key) == (
        "claude",
        "claude-code",
        model.claude_code.MODEL,
        "claude-token",
    )


def test_minimax_is_an_api_llm_with_its_address_key_and_the_agent_s_model(served):
    found = served(LLM_CHOICE="minimax")
    assert found == model.LLM("minimax", "anthropic-api", "MiniMax-M3", "https://mm.example", "mm-key")


def test_an_extra_global_llm_is_picked_by_name_and_a_removed_one_is_claude(served, monkeypatch):
    monkeypatch.setattr(model.cabinets, "llm_extras", lambda: EXTRAS)
    assert served(LLM_CHOICE="opus") == model.LLM(
        "opus", "anthropic-api", "claude-opus", "https://api.example", "k-opus"
    )
    monkeypatch.setattr(model.cabinets, "llm_extras", lambda: {})  # removed
    assert served(LLM_CHOICE="opus").name == "claude"


def test_a_cabinet_s_own_llm_is_its_own_and_defaults_to_the_first_type(served):
    found = served(LLM_CHOICE="custom", LLM_MODEL="haiku", LLM_KEY="own-token")
    assert (found.name, found.type, found.model, found.key) == ("custom", "claude-code", "haiku", "own-token")
    api = served(
        "cabinet_b", LLM_CHOICE="custom", LLM_TYPE="anthropic-api", LLM_MODEL="m", LLM_URL="https://x", LLM_KEY="k"
    )
    assert (api.type, api.url) == ("anthropic-api", "https://x")


@pytest.mark.security
def test_a_cabinet_never_reads_another_cabinet_s_own_llm(served):
    served("cabinet_b", LLM_CHOICE="custom", LLM_MODEL="b-model", LLM_KEY="b-secret")
    found = served("cabinet_a")  # picked nothing: Claude, not cabinet_b's
    assert found.name == "claude" and "b-secret" not in repr(found)


def test_the_choices_are_claude_first_then_minimax_the_extras_and_custom(served, monkeypatch):
    monkeypatch.setattr(model.cabinets, "llm_extras", lambda: {"zeta": EXTRAS["opus"], "alpha": EXTRAS["opus"]})
    assert list(model.choices()) == ["claude", "minimax", "alpha", "zeta", "custom"]


def test_the_old_adapters_are_read_once_for_every_cabinet_not_at_each_call(monkeypatch):
    queries = []

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query):
            queries.append(query)
            return self

        def fetchall(self):
            return [("cabinet_a", "minimax")]

    monkeypatch.setattr(model.psycopg, "connect", lambda **kw: Db())
    monkeypatch.setitem(model._legacy, "next", 0.0)
    assert [model.chosen_adapter(name) for name in ("cabinet_a", "cabinet_b", "cabinet_a")] == [
        "minimax",
        "",
        "minimax",
    ]
    assert len(queries) == 1  # one read, whoever asks and however often


def test_a_postgres_that_cannot_be_read_keeps_the_last_rows_and_breaks_no_call(monkeypatch):
    monkeypatch.setitem(model._legacy, "rows", {"cabinet_a": "minimax"})
    monkeypatch.setitem(model._legacy, "next", 0.0)
    monkeypatch.setattr(model, "log", lambda line: None)

    def down(**kw):
        raise model.psycopg.OperationalError("down")

    monkeypatch.setattr(model.psycopg, "connect", down)
    assert model.chosen_adapter("cabinet_a") == "minimax"


@pytest.mark.functionality
def test_only_minimax_thinks_any_other_api_gets_a_plain_call(served, monkeypatch):
    from engine import agent

    monkeypatch.setattr(model.cabinets, "llm_extras", lambda: EXTRAS)
    for choice, thinks in (("minimax", True), ("opus", False)):
        monkeypatch.setenv("CABINET_A__LLM_CHOICE", choice)
        assert (agent.model("cabinet_a").thinking is not None) is thinks


@pytest.mark.functionality
def test_a_vision_call_to_another_api_sends_neither_thinking_nor_temperature(served, monkeypatch):
    monkeypatch.setattr(model.cabinets, "llm_extras", lambda: EXTRAS)
    monkeypatch.setenv("CABINET_A__LLM_CHOICE", "opus")
    sent = []

    def post(url, **kw):
        sent.append(kw["json"])
        return model.httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"a": 1}'}], "usage": {}},
            request=model.httpx.Request("POST", url),
        )

    monkeypatch.setattr(model.HTTP, "post", post)
    with cabinets.using("cabinet_a"):
        assert model.ask_model("s", [], "i", "Lecture", keys=("a",)) == {"a": 1}
    assert sent[0]["model"] == "claude-opus" and "thinking" not in sent[0] and "temperature" not in sent[0]
