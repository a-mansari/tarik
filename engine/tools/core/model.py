"""The vision model (MiniMax-M3), in the Anthropic message format it imitates, and the pages of the documents
clients send, as images it can read."""

import base64
import io
import json
import re

import httpx
import psycopg
import pypdfium2
from PIL import Image, ImageOps

from engine.tools.core import claude_code
from engine.tools.core.app import ToolError, log
from engine.tools.core.cabinets import current, setting
from engine.tools.core.config import MAX_PAGES, MAX_SIDE, MODEL_TIMEOUT, POSTGRES, SCALE
from engine.tools.core.experio import HTTP
from engine.tools.core.queries import Q

# Where the content is read: its latest version, edited in the UI (engine/content.py sets it). Read at each use, so
# an edit applies to the next call.
SOURCE = None


def content(path):
    """A content document's text (tools/<name>.md, agent.json): its latest version."""
    if SOURCE is None:
        raise RuntimeError("content not loaded (engine.content)")
    return SOURCE(path)


def front_matter(text):
    """A Markdown document's front matter ({key: value}) and body; ValueError without one."""
    found = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, flags=re.S)
    if not found:
        raise ValueError("l'en-tête (entre les deux lignes ---) a disparu")
    meta = dict(line.split(": ", 1) for line in found.group(1).splitlines() if ": " in line)
    return meta, found.group(2)


def tool_prompt(name):
    """The prompt tools/<name>.md holds, after its front matter (used_by: the tool reading it)."""
    return front_matter(content(f"tools/{name}.md"))[1]


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


MODEL_URL = "https://api.minimax.io/anthropic"  # the provider's Anthropic-style address (MODEL_API_URL overrides it)

# The models a cabinet may work with (MODEL_ADAPTER, the Cabinets page): the first one when it names none.
ADAPTERS = {
    "minimax": "MiniMax-M3, par l'API de MiniMax (MODEL_API_KEY)",
    "claude-code": f"Claude ({claude_code.MODEL}), par la CLI Claude Code (CLAUDE_CODE_OAUTH_TOKEN)",
}


def chosen_adapter(cabinet):
    """The model ``cabinet`` picked on the Cabinets page ("" when none): in the orchestrator's table, not in Vault (it
    is no secret), nor in the cabinet's schema (no tool reads it, nor the agent)."""
    with psycopg.connect(**POSTGRES) as db:
        found = db.execute(Q.model_adapter, (cabinet,)).fetchone()
    return found[0] if found else ""


def adapter():
    """The current cabinet's model adapter (a key of ADAPTERS)."""
    chosen = chosen_adapter(current())
    return chosen if chosen in ADAPTERS else "minimax"


def model_name():
    """The model every tool uses: the agent's own (agent.json)."""
    return json.loads(content("agent.json"))["model"]


def ask_model(system, pages, instruction, what, think=True, keys=()):
    """The model's JSON answer (dict) to ``instruction`` about ``pages`` (PNG bytes, in order, maybe none).
    ``what`` names the step in the log and in the agent's ToolError when it fails. ``think``: M3 thinks first.
    ``keys``: the ones the answer must hold (json_answer)."""
    if adapter() == "claude-code":
        return ask_claude_code(system, pages, instruction, what, keys)
    key = setting("MODEL_API_KEY", "")
    if not key:
        raise ToolError(f"{what} : le modele n'est pas encore configure.")
    blocks = [*images_of(pages), {"type": "text", "text": instruction}]
    try:
        body = {
            "model": model_name(),
            "max_tokens": 16384,  # the thinking included: M3 thinks as long as it needs, 1k to 8k tokens
            "system": system,
            "messages": [{"role": "user", "content": blocks}],
        }
        if think:
            body["thinking"] = {"type": "adaptive"}
        else:
            body["temperature"] = 0
        answer = HTTP.post(
            setting("MODEL_API_URL", MODEL_URL).rstrip("/") + "/v1/messages",
            timeout=MODEL_TIMEOUT,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json=body,
        )
        answer.raise_for_status()
        text = "".join(block.get("text", "") for block in answer.json()["content"])
        return json_answer(text, keys)
    except httpx.HTTPStatusError as error:
        log(f"{what}: {error}: {error.response.text[:500]}")
        raise ToolError(f"{what} : {refused(error.response.status_code)}") from None
    except httpx.TransportError as error:
        log(f"{what}: {error!r}")
        raise ToolError(f"{what} : {READER} ne repond pas ; reessayez dans quelques minutes.") from None
    except Exception as error:
        log(f"{what}: {error!r}")
        raise ToolError(f"{what} : {READER} a donne une reponse illisible ; reessayez dans quelques minutes.") from None


def ask_claude_code(system, pages, instruction, what, keys):
    """ask_model through the Claude Code CLI (it does not think on demand: ``think`` has no use here)."""
    blocks = [*images_of(pages), {"type": "text", "text": instruction}]
    try:
        return json_answer(claude_code.ask(system, blocks), keys)
    except claude_code.ClaudeCodeError as error:
        log(f"{what}: {error}")
        raise ToolError(f"{what} : {READER} est indisponible ; reessayez dans quelques minutes.") from None
    except ValueError as error:
        log(f"{what}: {error!r}")
        raise ToolError(f"{what} : {READER} a donne une reponse illisible ; reessayez dans quelques minutes.") from None


def images_of(pages):
    """``pages`` (PNG bytes) as Anthropic image blocks, in order."""
    images = []
    for page in pages:
        source = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(page).decode()}
        images.append({"type": "image", "source": source})
    return images


# Who failed, said to the client: Tarik's own reading service, not Experio, and not the file's reception.
READER = "le service de lecture des documents de Tarik (pas Experio)"


def refused(status):
    """Why the model's service answered ``status``, for the client: the file refused (4xx: the same file fails
    again), the service down (5xx, 429: a later try may pass)."""
    if status in (429, 529) or status >= 500:
        return f"{READER} est indisponible (erreur {status}) ; reessayez dans quelques minutes."
    return (
        f"{READER} a refuse de lire ce fichier (erreur {status}), bien qu'il ait ete recu ; renvoyez-le en PDF. "
        "L'administrateur de Tarik peut voir le detail dans le journal du serveur."
    )


# --- The documents' pages ---


def to_pdf(raw):
    """A photo (image bytes) as one-page PDF bytes: Experio only takes PDFs. Turned upright as the phone took it
    (its EXIF orientation), on a page the size of an A4 whatever its pixels."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=max(image.size) / 11.69)  # 11.69 inches: an A4's long side
    return buffer.getvalue()


def pdf_pages(raw, limit=MAX_PAGES, scale=SCALE):
    """The first ``limit`` pages (None: all) of a PDF (bytes), as PNG bytes rendered at ``scale``, their longest
    side at most MAX_SIDE pixels."""
    pages = []
    for page in list(pypdfium2.PdfDocument(raw))[:limit]:
        fitting = min(scale, MAX_SIDE / max(page.get_size()))  # the longest side at most MAX_SIDE
        buffer = io.BytesIO()
        page.render(scale=fitting).to_pil().save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages


def scan_pages(document_pdf):
    """The first MAX_PAGES pages of a scan stored in Experio (base64 str), as PNG bytes."""
    return pdf_pages(base64.b64decode(document_pdf))
