"""The Claude Code CLI as the model (MODEL_ADAPTER claude-code): what it is sent, and its answers read back, a fake
`claude` on the PATH standing in for it."""

import json
import os
import stat

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from engine.claude_code import ChatClaudeCode
from tarik.core import cabinets, claude_code, model
from tarik.core.app import ToolError


def fake_cli(tmp_path, monkeypatch, result):
    """A `claude` that saves its arguments and stdin, then answers with the ``result`` event."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {tmp_path}/args\ncat > {tmp_path}/stdin\n'
        f"echo '{{\"type\": \"system\"}}'\necho '{json.dumps({'type': 'result', **result})}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"cabinet_a"}))
    monkeypatch.setenv("CABINET_A__MODEL_ADAPTER", "claude-code")


@pytest.mark.technical
def test_the_vision_model_goes_through_the_cli_when_the_cabinet_picks_it(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"is_error": False, "result": 'Voici : {"kind": "facture"}'})
    with cabinets.using("cabinet_a"):
        assert model.ask_model("sys", [b"png"], "Quoi ?", "Identification", keys=("kind",)) == {"kind": "facture"}
    sent = json.loads((tmp_path / "stdin").read_text())["message"]["content"]
    assert sent[0]["type"] == "image" and sent[-1] == {"type": "text", "text": "Quoi ?"}
    assert "--tools" in (tmp_path / "args").read_text()  # its own tools off


@pytest.mark.communication
def test_a_cli_out_of_quota_says_tarik_s_reader_is_unavailable(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"is_error": True, "result": "usage limit reached"})
    with cabinets.using("cabinet_a"), pytest.raises(ToolError, match="indisponible"):
        model.ask_model("sys", [], "Quoi ?", "Identification")


@pytest.mark.technical
def test_the_agent_gets_the_cli_s_tool_calls_and_the_cli_the_whole_conversation(tmp_path, monkeypatch):
    @tool
    def find_dossier(name: str) -> str:
        """Find a dossier."""
        return name

    calls = [{"name": "find_dossier", "args": {"name": "first"}}, {"name": "rm", "args": {}}]  # rm: no such tool
    answer = {"text": "", "tool_calls": calls}
    fake_cli(tmp_path, monkeypatch, {"is_error": False, "result": "", "structured_output": answer})
    chat = ChatClaudeCode(cabinet="cabinet_a").bind_tools([find_dossier])
    conversation = [
        SystemMessage("Tu es Tarik."),
        HumanMessage("Bonjour"),
        AIMessage("", tool_calls=[{"name": "find_dossier", "args": {"name": "x"}, "id": "t1"}]),
        ToolMessage("aucun", tool_call_id="t1"),
    ]
    reply = chat.invoke(conversation)
    assert [(call["name"], call["args"]) for call in reply.tool_calls] == [("find_dossier", {"name": "first"})]
    sent = json.loads((tmp_path / "stdin").read_text())["message"]["content"]
    words = " ".join(block.get("text", "") for block in sent)
    assert "Bonjour" in words and "[tool call t1]" in words and "aucun" in words
    arguments = (tmp_path / "args").read_text()
    assert "Tu es Tarik." in arguments and "find_dossier" in arguments and "--json-schema" in arguments


@pytest.mark.technical
def test_an_unknown_adapter_falls_back_to_minimax(monkeypatch):
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({"cabinet_a"}))
    monkeypatch.setenv("CABINET_A__MODEL_ADAPTER", "gpt")
    with cabinets.using("cabinet_a"):
        assert model.adapter() == "minimax"
    assert claude_code.MODEL
