"""The vision model (MiniMax-M3), in the Anthropic message format it imitates."""

import base64
import json

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.cabinets import setting
from tarik.core.experio import HTTP


def ask_model(system, pages, instruction, what):
    """MiniMax-M3's JSON answer about some page images (PNG bytes, in order): hence the /v1/messages
    path and the anthropic-version header. ``what`` names the step in the log and in the agent's error."""
    url, key, model = (setting(name, "") for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_API_NAME"))
    if not (url and key and model):
        raise ToolError(f"{what} : le modele n'est pas encore configure.")
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(page).decode()}} for page in pages]
    content.append({"type": "text", "text": instruction})
    try:
        answer = HTTP.post(f"{url.rstrip('/')}/v1/messages", timeout=300, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01"}, json={
            "model": model, "max_tokens": 8192, "temperature": 0, "system": system, "messages": [{"role": "user", "content": content}]})
        answer.raise_for_status()
        text = "".join(block.get("text", "") for block in answer.json()["content"])
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception as error:
        print(f"{what}: {error}", flush=True)
        raise ToolError(f"{what} : echec, reessayez plus tard.") from None
