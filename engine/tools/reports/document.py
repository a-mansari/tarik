"""A document as Tarik builds it: a title and blocks, validated once, then written in any format (formats.py).

The agent gives the blocks as plain data, never markup, so no format ever carries what the model or a client typed as
code (a formula in a cell, a LaTeX command, HTML): each renderer escapes its own way. A block is a dict:

- {"type": "heading", "level": 1 to 3, "text"}
- {"type": "paragraph", "text"}; {"type": "note", "text"} (a remark set apart: a warning, how the figures were made)
- {"type": "bullets" or "numbered", "items": [text, ...]}
- {"type": "table", "headers": [text, ...], "rows": [[cell, ...], ...], "caption"?} (a cell is text, a number or empty)

Limits keep a file within what a mail takes and the server's memory."""

import re
from dataclasses import dataclass, field

from engine.tools.core.app import ToolError

MAX_BLOCKS, MAX_TABLES = 300, 20
MAX_TEXT, MAX_TITLE, MAX_ITEMS = 4000, 150, 200
MAX_COLUMNS, MAX_ROWS, MAX_CELLS = 20, 2000, 10_000  # PDF and Word take about 3 s per 10 000 cells
FORMATS = ("md", "docx", "xlsx", "pdf", "tex")
KINDS = {"heading", "paragraph", "note", "bullets", "numbered", "table"}
# What no format should carry in text: control characters (all but tab and line feed, carriage returns go too), the
# characters XML forbids (Word and Excel refuse them), and those that reverse or hide text (bidi overrides and isolates,
# zero-width characters, the byte-order mark): a client's text must read in the document as it was stored.
CONTROL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff\ufdd0-\ufdef\u200b-\u200d\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)
MAX_NUMBER = 10**15


@dataclass
class Document:
    """A validated document: ``blocks`` are normalised dicts (text as str, a table's cells all str)."""

    title: str
    subtitle: str = ""
    blocks: list = field(default_factory=list)


def text_of(value, where, limit=MAX_TEXT):
    """Return ``value`` as clean text: numbers as written, None as "", control characters out. Raises ToolError naming
    ``where`` for something else than text or a number, or text over ``limit`` characters."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ToolError(f"{where} : du texte ou un nombre attendu.")
    if isinstance(value, (int, float)) and not -MAX_NUMBER < value < MAX_NUMBER:  # also NaN: str(10**5000) would raise
        raise ToolError(f"{where} : un nombre trop grand.")
    text = CONTROL.sub("", str(value).replace("\r\n", "\n").replace("\r", "\n")).strip()
    text = text.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate is no text any format can hold
    if len(text) > limit:
        raise ToolError(f"{where} : {limit} caractères au plus.")
    return text


def as_list(value, where, limit):
    """Return ``value`` as a list of at most ``limit`` entries. Raises ToolError naming ``where`` otherwise."""
    if not isinstance(value, list):
        raise ToolError(f"{where} : une liste attendue.")
    if len(value) > limit:
        raise ToolError(f"{where} : {limit} éléments au plus.")
    return value


def table_of(block, where, cells):
    """Return the table ``block`` normalised and the number of cells it adds to ``cells``. Raises ToolError."""
    headers = [
        text_of(h, f"{where}, en-tête", 200) for h in as_list(block.get("headers"), f"{where}, en-têtes", MAX_COLUMNS)
    ]
    rows = [
        [text_of(cell, f"{where}, ligne {n}", 500) for cell in as_list(row, f"{where}, ligne {n}", MAX_COLUMNS)]
        for n, row in enumerate(as_list(block.get("rows"), f"{where}, lignes", MAX_ROWS), 1)
    ]
    if not headers:
        raise ToolError(f"{where} : des en-têtes sont nécessaires.")
    for n, row in enumerate(rows, 1):
        if len(row) != len(headers):
            raise ToolError(f"{where}, ligne {n} : {len(headers)} cellules attendues, {len(row)} reçues.")
    return {"type": "table", "headers": headers, "rows": rows, "caption": text_of(block.get("caption"), where, 150)}, (
        cells + len(headers) + sum(len(row) for row in rows)
    )


def parse(title, blocks, subtitle=""):
    """Return the Document for ``title``, ``blocks`` (a list of dicts, see above) and ``subtitle``. Raises ToolError, in
    plain French and naming the block's number, for anything the formats could not take."""
    title = text_of(title, "Titre", MAX_TITLE)
    if not title:
        raise ToolError("Titre : un titre est nécessaire.")
    found, tables, cells = [], 0, 0
    for n, block in enumerate(as_list(blocks, "Contenu", MAX_BLOCKS), 1):
        where = f"Bloc {n}"
        kind = block.get("type") if isinstance(block, dict) else None
        if not isinstance(kind, str) or kind not in KINDS:
            raise ToolError(f"{where} : un type parmi {', '.join(sorted(KINDS))} attendu.")
        if kind == "table":
            tables += 1
            if tables > MAX_TABLES:
                raise ToolError(f"Contenu : {MAX_TABLES} tableaux au plus.")
            table, cells = table_of(block, where, cells)
            if cells > MAX_CELLS:
                raise ToolError(f"Contenu : {MAX_CELLS} cellules au plus en tout.")
            found.append(table)
        elif kind in ("bullets", "numbered"):
            items = [text_of(i, f"{where}, élément", 1000) for i in as_list(block.get("items"), where, MAX_ITEMS)]
            found.append({"type": kind, "items": [item for item in items if item]})
        elif kind == "heading":
            level = block.get("level", 1)
            if type(level) is not int or level not in (1, 2, 3):
                raise ToolError(f"{where} : un niveau de titre de 1 à 3 attendu.")
            found.append({"type": kind, "level": level, "text": text_of(block.get("text"), where, 300)})
        else:
            found.append({"type": kind, "text": text_of(block.get("text"), where)})
    return Document(title, text_of(subtitle, "Sous-titre", MAX_TITLE), found)
