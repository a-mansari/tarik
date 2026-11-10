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
