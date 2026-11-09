"""Tarik_document's tool: write a document (document.py) in the formats asked (formats.py) and keep each file in the
conversation's outbox (core/outbox.py), where the next email reply attaches it and the chat offers it for download.

The text and the figures are the agent's: this tool writes what it is given and never reads Experio, so it adds no
number of its own. All the files are made before any is kept: a refusal leaves nothing half done."""

import re
import unicodedata

from engine.tools.core.app import ToolError, tool
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.outbox import keep_file
from engine.tools.core.queries import Q
from engine.tools.reports.document import FORMATS, parse
from engine.tools.reports.formats import MAX_BYTES, RENDERERS

NAMES = {"docx": "Word", "xlsx": "Excel", "pdf": "PDF", "md": "Markdown", "tex": "LaTeX"}
WAITING_FILES, WAITING_BYTES = 15, 15 * 1024 * 1024  # what a conversation's outbox may hold before a reply sends it


def stem_of(wanted):
    """Return a file name stem from ``wanted``: ASCII letters, digits and hyphens, at most 60 long, never empty."""
    plain = unicodedata.normalize("NFKD", wanted).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "-", plain).strip("-").lower()[:60] or "document"


def size_of(data):
    return f"{max(1, round(len(data) / 1024))} Ko"


@tool
def make_document(
    session_id: str, title: str, blocks: list, formats: list, subtitle: str = "", filename: str = ""
) -> str:
    """Write a document as ``formats`` (a list among docx, xlsx, pdf, md, tex) and keep the files in conversation
    ``session_id``'s outbox: the next email reply attaches them, the chat offers them for download. ``blocks`` is the
    content (document.py describes each block), plain text only. ``filename``: the files' name without extension
    (else the title's).

    Returns French text saying which files were made, and what the reader should know about them (a PDF holds only
    the Latin alphabet; LaTeX is source to compile). Raises ToolError, in plain French, for an unknown format or a
    document the formats could not take (document.py's limits), a file over MAX_BYTES, or an outbox already holding
    too much that no reply has sent (WAITING_FILES, WAITING_BYTES)."""
    if not session_id:
        raise ToolError("Aucune conversation où garder le document.")
    wanted = list(dict.fromkeys(str(f).strip().lower() for f in formats)) if isinstance(formats, list) else []
    unknown = [f for f in wanted if f not in FORMATS]
    if not wanted or unknown:
        raise ToolError(
            f"Format : un ou plusieurs parmi {', '.join(FORMATS)}" + (f" (pas « {unknown[0]} »)." if unknown else ".")
        )
    document = parse(title, blocks, subtitle)
    stem = stem_of(filename or document.title)
    made, notes = [], []
    for fmt in wanted:
        data, found = RENDERERS[fmt](document)
        if len(data) > MAX_BYTES:
            raise ToolError(
                f"Le fichier {NAMES[fmt]} dépasse {MAX_BYTES // (1024 * 1024)} Mo : raccourcissez le document."
            )
        made.append((f"{stem}.{fmt}", data))
        notes += found
    with cabinet_db() as db:
        count, size = db.execute(Q.outbox_waiting, (session_id,)).fetchone()
    if count + len(made) > WAITING_FILES or size + sum(len(data) for _, data in made) > WAITING_BYTES:
        raise ToolError(
            f"Trop de fichiers attendent déjà d'être envoyés dans cette conversation ({count}) : "
            "ils partiront avec ma prochaine réponse, refaites ce document ensuite."
        )
    for name, data in made:
        keep_file(session_id, name, data)
    files = ", ".join(f"{name} ({size_of(data)})" for name, data in made)
    text = (
        f"Fichiers prêts : {files}. Ils seront joints à ma prochaine réponse par email, "
        "et téléchargeables dans la discussion."
    )
    return text + "".join(f"\n- {note}" for note in dict.fromkeys(notes))
