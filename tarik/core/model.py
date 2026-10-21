"""The vision model (MiniMax-M3), in the Anthropic message format it imitates, and the pages of the documents
clients send, as images it can read."""

import base64
import io
import json
import re

import httpx
import pypdfium2
from PIL import Image, ImageOps

from tarik.core.app import ToolError, log
from tarik.core.cabinets import setting
from tarik.core.config import MAX_PAGES, MAX_SIDE, MODEL_TIMEOUT, SCALE
from tarik.core.experio import HTTP

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


def model_name():
    """The model every tool uses: the agent's own (agent.json)."""
    return json.loads(content("agent.json"))["model"]


def ask_model(system, pages, instruction, what, think=True, keys=()):
    """The model's JSON answer (dict) to ``instruction`` about ``pages`` (PNG bytes, in order, maybe none).
    ``what`` names the step in the log and in the agent's ToolError when it fails. ``think``: M3 thinks first.
    ``keys``: the ones the answer must hold (json_answer)."""
    key = setting("MODEL_API_KEY", "")
    if not key:
        raise ToolError(f"{what} : le modele n'est pas encore configure.")
    png = [{"type": "base64", "media_type": "image/png", "data": base64.b64encode(page).decode()} for page in pages]
    images = [{"type": "image", "source": source} for source in png]
    content = [*images, {"type": "text", "text": instruction}]
    try:
        answer = HTTP.post(
            setting("MODEL_API_URL", MODEL_URL).rstrip("/") + "/v1/messages",
            timeout=MODEL_TIMEOUT,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": model_name(),
                "max_tokens": 16384,  # the thinking included: M3 thinks as long as it needs, 1k to 8k tokens
                "system": system,
                "messages": [{"role": "user", "content": content}],
                **({"thinking": {"type": "adaptive"}} if think else {"temperature": 0}),
            },
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
        buffer = io.BytesIO()
        page.render(scale=min(scale, MAX_SIDE / max(page.get_size()))).to_pil().save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages


def scan_pages(document_pdf):
    """The first MAX_PAGES pages of a scan stored in Experio (base64 str), as PNG bytes."""
    return pdf_pages(base64.b64decode(document_pdf))
