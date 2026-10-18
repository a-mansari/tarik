"""The vision model (MiniMax-M3), in the Anthropic message format it imitates."""

import base64
import json
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import log
from tarik.core.cabinets import setting
from tarik.core.config import MODEL_TIMEOUT
from tarik.core.experio import HTTP

INSTRUCTIONS = Path(__file__).resolve().parents[3] / "instructions" / "tools"  # the repository's, / in the container


def instruction(name):
    """The prompt instructions/tools/<name>.md holds, after its front matter (used_by: the tool reading it)."""
    return (INSTRUCTIONS / f"{name}.md").read_text().split("\n---\n", 1)[1]


def json_answer(text, keys=()):
    """The one JSON object in the model's ``text`` holding every one of ``keys``, whatever notes surround it.
    ValueError when there is none, or several that differ: never a guess between two answers."""
    found, decoder, start = [], json.JSONDecoder(), text.find("{")
    while start != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:  # a brace in the notes ("{rate: 10}"): try the next one
            end = start + 1
        else:
            if isinstance(value, dict) and all(key in value for key in keys) and value not in found:
                found.append(value)
        start = text.find("{", end)
    if len(found) != 1:
        raise ValueError(f"{len(found)} answers with {list(keys)} in: {text[:200]!r}")
    return found[0]


def ask_model(system, pages, instruction, what, think=True, keys=()):
    """MiniMax-M3's JSON answer (dict) to ``instruction`` about ``pages`` (PNG bytes, in order, maybe none).
    ``what`` names the step in the log and in the agent's ToolError when it fails. ``think``: M3 thinks first.
    ``keys``: the ones the answer must hold (json_answer)."""
    url, key, model = (setting(name, "") for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_API_NAME"))
    if not (url and key and model):
        raise ToolError(f"{what} : le modele n'est pas encore configure.")
    png = [{"type": "base64", "media_type": "image/png", "data": base64.b64encode(page).decode()} for page in pages]
    images = [{"type": "image", "source": source} for source in png]
    content = [*images, {"type": "text", "text": instruction}]
    try:
        answer = HTTP.post(
            f"{url.rstrip('/')}/v1/messages",  # the Anthropic path and header MiniMax imitates
            timeout=MODEL_TIMEOUT,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "max_tokens": 16384,  # the thinking included: M3 thinks as long as it needs, 1k to 8k tokens
                "system": system,
                "messages": [{"role": "user", "content": content}],
                **({"thinking": {"type": "adaptive"}} if think else {"temperature": 0}),
            },
        )
        answer.raise_for_status()
        text = "".join(block.get("text", "") for block in answer.json()["content"])
        return json_answer(text, keys)
    except Exception as error:
        log(f"{what}: {error}")
        raise ToolError(f"{what} : echec, reessayez plus tard.") from None
