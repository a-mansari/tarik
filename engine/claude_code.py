"""The agent's model through the Claude Code CLI (engine/tools/core/claude_code.py), as a LangChain chat model.

The CLI's own tools are off: the model writes its tool calls as structured output, so LangGraph still runs every
tool, keeps the conversation and logs each call as it does with the API. The conversation goes in as one message,
each turn headed by its role, images and PDFs kept as content blocks."""

import asyncio
import json
import uuid

from langchain_anthropic.chat_models import _format_messages, convert_to_anthropic_tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from engine.tools.core import claude_code
from engine.tools.core.cabinets import using

PROTOCOL = """

<tool_protocol>
You cannot run tools yourself. To use some, answer with "tool_calls": each {"name", "args"}, the args matching the
tool's input_schema; their results come back in the next message. When you need no tool, answer the client in "text"
with "tool_calls" empty. The tools:
{tools}
</tool_protocol>"""


def answer_schema(tools):
    """Return the JSON schema of the CLI's answer: {"text", "tool_calls": [{"name", "args"}]}, each call one of
    ``tools`` (Anthropic tool dicts) with its args matching that tool's input_schema; any call without tools."""
    calls = [
        {
            "type": "object",
            "properties": {"name": {"const": found["name"]}, "args": found["input_schema"]},
            "required": ["name", "args"],
        }
        for found in tools
    ]
    item = {"anyOf": calls} if calls else {
        "type": "object",
        "properties": {"name": {"type": "string"}, "args": {"type": "object"}},
        "required": ["name", "args"],
    }  # fmt: skip
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}, "tool_calls": {"type": "array", "items": item}},
        "required": ["text", "tool_calls"],
    }


def written(block):
    """Return a content block as the CLI's single message carries it: images and PDFs unchanged, files, tool calls
    and tool results as text; None for thinking blocks (not sent again)."""
    kind = block.get("type") if isinstance(block, dict) else "text"
    if kind in ("image", "document"):
        return block
    if kind == "text":
        return {"type": "text", "text": block["text"] if isinstance(block, dict) else str(block)}
    if kind == "file":
        named = f"{block.get('file_name', '?')} ({block.get('mime_type', '')})"
        return {"type": "text", "text": f"[fichier joint : {named}]"}
    if kind == "tool_use":
        call = json.dumps({"name": block["name"], "args": block["input"]}, ensure_ascii=False)
        return {"type": "text", "text": f"[tool call {block['id']}] {call}"}
    if kind == "tool_result":
        result = block.get("content")
        if isinstance(result, list):
            result = "\n".join(part.get("text", "") for part in result if isinstance(part, dict))
        return {"type": "text", "text": f"[tool result {block['tool_use_id']}]\n{result}"}
    return None  # thinking: the model's own, not sent again


def flattened(messages):
    """Return the conversation (Anthropic-format messages) as one message's content blocks, each turn headed by
    "=== role ===" ."""
    content = []
    for message in messages:
        content.append({"type": "text", "text": f"=== {message['role']} ==="})
        blocks = message["content"] if isinstance(message["content"], list) else [message["content"]]
        content += [found for found in map(written, blocks) if found]
    return content


class ChatClaudeCode(BaseChatModel):
    """Chat model answering through the Claude Code CLI, logged in with ``cabinet``'s settings.

    Tool calls naming a tool it was not given are dropped; each kept call gets a new toolu_ id."""

    cabinet: str  # the settings (the CLI's login) are read within it

    @property
    def _llm_type(self):
        return "claude-code-cli"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_anthropic_tool(found) for found in tools])

    def _generate(self, messages, stop=None, run_manager=None, tools=(), **kwargs):
        system, conversation = _format_messages(messages, model=None)
        if isinstance(system, list):
            system = "\n".join(part.get("text", "") for part in system)
        listed = "\n".join(json.dumps(found, ensure_ascii=False) for found in tools)
        names = [found["name"] for found in tools]
        with using(self.cabinet):
            answer = claude_code.ask(
                (system or "") + PROTOCOL.replace("{tools}", listed or "(none)"),
                flattened(conversation),
                answer_schema(tools),
            )
        calls = [
            {"name": call["name"], "args": call.get("args") or {}, "id": f"toolu_{uuid.uuid4().hex[:24]}"}
            for call in answer.get("tool_calls") or []
            if call.get("name") in names
        ]
        message = AIMessage(content=answer.get("text") or "", tool_calls=calls)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return await asyncio.to_thread(self._generate, messages, stop, None, **kwargs)
