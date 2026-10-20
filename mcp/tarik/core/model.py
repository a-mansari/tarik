"""The vision model (MiniMax-M3), in the Anthropic message format it imitates, and the pages of the documents
clients send, as images it can read."""

import base64
import io
import json
import time
from pathlib import Path

import pypdfium2
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

from tarik.core.app import log
from tarik.core.cabinets import current, setting
from tarik.core.config import MAX_PAGES, MODEL_TIMEOUT, SCALE
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


# "Tarik - Model" (workflows/build_tarik_model.py), on Docker's network: n8n sends the request on with the agents'
# own credential, so the model's key stays in n8n.
MODEL_HOOK = "http://n8n:5678/webhook/tarik-model"
HOOK_HEADER = "X-Tarik-Webhook"  # the secret tarik-mcp and n8n share (Vault MAIL_WEBHOOK_SECRET)
MODEL_KEPT_SECONDS = 300
_models = {}  # cabinet: (the agent's model, when to ask n8n again)


def model_name():
    """The model the current cabinet's agent runs on in n8n, without its provider ("minimax/MiniMax-M3" gives
    "MiniMax-M3"): the model chosen for the agents is the one every tool uses. Asked of n8n, kept 5 minutes;
    when n8n can't answer, the last one. ToolError when there is none."""
    from tarik.emails.agent import call_n8n  # imported here: core does not import emails at import time

    cabinet = current()
    known = _models.get(cabinet)
    if known and time.monotonic() < known[1]:
        return known[0]
    try:
        model = call_n8n("get_agent", agentId=setting("AGENT_ID"))["config"]["model"].split("/", 1)[-1]
    except Exception as error:
        log(f"model_name {cabinet}: {error}")
        if known:
            return known[0]
        raise ToolError("Le modele de l'agent est inconnu pour le moment, reessayez plus tard.") from None
    _models[cabinet] = (model, time.monotonic() + MODEL_KEPT_SECONDS)
    return model


def ask_model(system, pages, instruction, what, think=True, keys=()):
    """The agents' model's (model_name) JSON answer (dict) to ``instruction`` about ``pages`` (PNG bytes, in order,
    maybe none).
    ``what`` names the step in the log and in the agent's ToolError when it fails. ``think``: M3 thinks first.
    ``keys``: the ones the answer must hold (json_answer)."""
    secret = setting("MAIL_WEBHOOK_SECRET", "")
    if not secret:
        raise ToolError(f"{what} : le modele n'est pas encore configure.")
    model = model_name()
    png = [{"type": "base64", "media_type": "image/png", "data": base64.b64encode(page).decode()} for page in pages]
    images = [{"type": "image", "source": source} for source in png]
    content = [*images, {"type": "text", "text": instruction}]
    try:
        answer = HTTP.post(
            MODEL_HOOK,
            timeout=MODEL_TIMEOUT,
            headers={HOOK_HEADER: secret},
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


# --- The documents' pages ---


def to_pdf(raw):
    """A photo (image bytes) as one-page PDF bytes: Experio only takes PDFs."""
    buffer = io.BytesIO()
    Image.open(io.BytesIO(raw)).convert("RGB").save(buffer, format="PDF")
    return buffer.getvalue()


def pdf_pages(raw, limit=MAX_PAGES, scale=SCALE):
    """The first ``limit`` pages (None: all) of a PDF (bytes), as PNG bytes rendered at ``scale``."""
    pages = []
    for page in list(pypdfium2.PdfDocument(raw))[:limit]:
        buffer = io.BytesIO()
        page.render(scale=scale).to_pil().save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages


def scan_pages(document_pdf):
    """The first MAX_PAGES pages of a scan stored in Experio (base64 str), as PNG bytes."""
    return pdf_pages(base64.b64decode(document_pdf))
