"""The vision model the tools ask (MiniMax-M3 through its Anthropic-style API, or Claude through the Claude Code CLI,
per cabinet), and the pages of the documents clients send, rendered as images it can read."""

import base64
import io
import json
import multiprocessing
import os
import re
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import httpx
import psycopg
from PIL import Image, ImageOps

from engine.tools.core import claude_code, pdfs, usage
from engine.tools.core.app import ToolError, log
from engine.tools.core.cabinets import current, setting
from engine.tools.core.config import MAX_PAGES, MAX_SIDE, MODEL_TIMEOUT, PDF_TIMEOUT, POSTGRES, SCALE
from engine.tools.core.experio import HTTP
from engine.tools.core.queries import Q

# Where the content is read: its latest version, edited in the UI (engine/content.py sets it). Read at each use, so
# an edit applies to the next call.
SOURCE = None


def content(path):
    """Return the latest text of content document ``path`` (tools/<name>.md, agent.json). Raises RuntimeError
    before engine.content has set SOURCE."""
    if SOURCE is None:
        raise RuntimeError("content not loaded (engine.content)")
    return SOURCE(path)


def front_matter(text):
    """Split a Markdown document into (front matter {key: value}, body). Raises ValueError without front matter."""
    found = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, flags=re.S)
    if not found:
        raise ValueError("l'en-tête (entre les deux lignes ---) a disparu")
    meta = dict(line.split(": ", 1) for line in found.group(1).splitlines() if ": " in line)
    return meta, found.group(2)


def tool_prompt(name):
    """Return the prompt in tools/<name>.md, after its front matter (used_by: the tool reading it)."""
    return front_matter(content(f"tools/{name}.md"))[1]


def json_answer(text, keys=()):
    """Return the one JSON object (dict) in the model's ``text`` holding every one of ``keys``, whatever notes
    surround it. Raises ValueError when there is none, or several that differ: never a guess between two answers."""
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


# The provider's Anthropic-style address: the stack's default from .env (TARIK_MODEL_URL; not MODEL_API_URL, a Vault
# setting that must not also sit in the environment), which Vault's MODEL_API_URL (secret/tarik/orchestrator) overrides.
MODEL_URL = os.getenv("TARIK_MODEL_URL", "").strip()

# The models a cabinet may work with (MODEL_ADAPTER, the Cabinets page): the first one when it names none.
ADAPTERS = {
    "minimax": "MiniMax-M3, par l'API de MiniMax (MODEL_API_KEY)",
    "claude-code": f"Claude ({claude_code.MODEL}), par la CLI Claude Code (CLAUDE_CODE_OAUTH_TOKEN)",
}


def chosen_adapter(cabinet):
    """Return the model adapter ``cabinet`` picked on the Cabinets page ("" when none). It is stored in the
    orchestrator's table: not in Vault (it is no secret), nor in the cabinet's schema (no tool nor agent reads it)."""
    with psycopg.connect(**POSTGRES) as db:
        found = db.execute(Q.model_adapter, (cabinet,)).fetchone()
    return found[0] if found else ""


def adapter():
    """Return the current cabinet's model adapter (a key of ADAPTERS): its choice, else "minimax"."""
    chosen = chosen_adapter(current())
    return chosen if chosen in ADAPTERS else "minimax"


def model_name():
    """Return the model name every tool sends to the MiniMax API: the agent's own (agent.json)."""
    return json.loads(content("agent.json"))["model"]


def ask_model(system, pages, instruction, what, think=True, keys=()):
    """Return the model's JSON answer (dict) to ``instruction`` about ``pages``, through the cabinet's adapter.

    Args:
        system: the system prompt.
        pages: PNG bytes, in order (maybe none).
        what: the step's name, in the log and in the ToolError.
        think: let M3 think first (else temperature 0); unused by the Claude Code CLI.
        keys: the keys the answer must hold (json_answer).

    Raises ToolError when the model is not configured, refuses, can't be reached, or answers unreadably."""
    if adapter() == "claude-code":
        return ask_claude_code(system, pages, instruction, what, keys)
    key, url = setting("MODEL_API_KEY", ""), setting("MODEL_API_URL", MODEL_URL)
    if not key or not url:
        raise ToolError(f"{what} : le modèle n'est pas encore configuré.")
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
            url.rstrip("/") + "/v1/messages",
            timeout=MODEL_TIMEOUT,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json=body,
        )
        answer.raise_for_status()
        usage.anthropic_usage(body["model"], answer.json().get("usage") or {})
        text = "".join(block.get("text", "") for block in answer.json()["content"])
        return json_answer(text, keys)
    except httpx.HTTPStatusError as error:
        log(f"{what}: {error}: {error.response.text[:500]}")
        raise ToolError(f"{what} : {refused(error.response.status_code)}") from None
    except httpx.TransportError as error:
        log(f"{what}: {error!r}")
        raise ToolError(f"{what} : {READER} ne répond pas ; réessayez dans quelques minutes.") from None
    except Exception as error:
        log(f"{what}: {error!r}")
        raise ToolError(f"{what} : {READER} a donné une réponse illisible ; réessayez dans quelques minutes.") from None


def ask_claude_code(system, pages, instruction, what, keys):
    """Return ask_model's answer through the Claude Code CLI (it does not think on demand: no ``think``), a JSON
    object holding ``keys`` by the CLI's own schema check. Raises ToolError when the CLI fails or gives no answer."""
    blocks = [*images_of(pages), {"type": "text", "text": instruction}]
    try:
        return claude_code.ask(system, blocks, {"type": "object", "required": list(keys)})
    except claude_code.ClaudeCodeStopped:
        raise ToolError(f"{what} : arrêté avec son exécution.") from None
    except claude_code.ClaudeCodeError as error:
        log(f"{what}: {error}")
        unreadable = isinstance(error, claude_code.ClaudeCodeUnreadable)
        why = "a donné une réponse illisible" if unreadable else "est indisponible"
        raise ToolError(f"{what} : {READER} {why} ; réessayez dans quelques minutes.") from None


def images_of(pages):
    """Return ``pages`` (PNG bytes) as Anthropic image blocks, in order."""
    images = []
    for page in pages:
        source = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(page).decode()}
        images.append({"type": "image", "source": source})
    return images


# Who failed, said to the client: Tarik's own reading service, not Experio, and not the file's reception.
READER = "le service de lecture des documents de Tarik (pas Experio)"


def refused(status):
    """Return why the model's service answered HTTP ``status``, in French for the client: the service unavailable
    (5xx, 429, 529: a later try may pass), else the file refused (4xx: the same file fails again)."""
    if status in (429, 529) or status >= 500:
        return f"{READER} est indisponible (erreur {status}) ; réessayez dans quelques minutes."
    return (
        f"{READER} a refusé de lire ce fichier (erreur {status}), bien qu'il ait été reçu ; renvoyez-le en PDF. "
        "L'administrateur de Tarik peut voir le détail dans le journal du serveur."
    )


# --- The documents' pages ---


def to_pdf(raw):
    """Return photo ``raw`` (image bytes) as one-page PDF bytes, turned upright by its EXIF orientation, on a page
    the size of an A4 whatever its pixels. Experio only takes PDFs."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=max(image.size) / 11.69)  # 11.69 inches: an A4's long side
    return buffer.getvalue()


# pdfium is not thread-safe, not even for two documents, and a PDF can make it hang: every use runs in one of these
# processes (pdfs.py), several cabinets' pages rendered at once, each call's process killed after PDF_TIMEOUT of work
# (pdfs.within; time waiting in the queue does not count). A dead process breaks its pool, failing every call in it:
# the pool is replaced and each call tried once more, so only the PDF that broke it fails. A script whose __main__
# reads PDFs needs the `if __name__ == "__main__"` guard, as the pool's processes import it (python -m server's own
# is never imported).
PDF_WORKERS = 4
PDF_POOL = None
PDF_POOL_LOCK = threading.Lock()


def pdfium(function, *args):
    """Return ``function(*args)`` (one of pdfs.py's) run in a pool process; its own exceptions (pypdfium2.PdfiumError)
    are raised as they are. Raises ToolError when it works over PDF_TIMEOUT or its process dies, twice."""
    global PDF_POOL
    for _ in range(2):  # a pool another call's PDF broke: tried again on the new one
        try:
            with PDF_POOL_LOCK:  # submitted under the lock: never to a pool another call has just killed
                if PDF_POOL is None:  # forkserver, not fork: the server's other threads hold locks
                    PDF_POOL = ProcessPoolExecutor(PDF_WORKERS, mp_context=multiprocessing.get_context("forkserver"))
                pool = PDF_POOL
                future = pool.submit(pdfs.within, PDF_TIMEOUT, function, *args)
            return future.result()
        except BrokenProcessPool as error:
            log(f"pdfium {function.__name__}: {error!r}")
            with PDF_POOL_LOCK:
                if PDF_POOL is pool:  # once, whichever call saw it first
                    PDF_POOL = None
                    pool.kill_workers()  # the pool's other processes, and its shutdown
    raise ToolError("Le fichier PDF n'a pas pu être lu (fichier illisible), réessayez.") from None


def pdf_pages(raw, limit=MAX_PAGES, scale=SCALE):
    """Return the first ``limit`` pages (None: all) of PDF bytes ``raw`` as PNG bytes, rendered at ``scale`` with
    their longest side at most MAX_SIDE pixels. Raises pypdfium2.PdfiumError when ``raw`` is no PDF."""
    return pdfium(pdfs.pages, raw, limit, scale, MAX_SIDE)


def pdf_text(raw, pages=None):
    """Return the text of PDF ``raw``'s first ``pages`` pages (None: all), blanks squeezed (a RIB is printed in
    columns); "" for a scan, an image or an unreadable file (one pdfium hangs or crashes on too)."""
    try:
        return pdfium(pdfs.text, raw, pages)
    except ToolError:
        return ""


def scan_pages(document_pdf):
    """Return the first MAX_PAGES pages of a scan stored in Experio (``document_pdf``: base64 str), as PNG bytes."""
    return pdf_pages(base64.b64decode(document_pdf))
