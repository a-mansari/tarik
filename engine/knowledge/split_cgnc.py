"""Split the CGNC (Code Général de Normalisation Comptable, Morocco) into the pieces Tarik searches, word for word,
and its chart of accounts (modèle normal), written to engine/knowledge/cgnc/ (pieces.jsonl, accounts.jsonl).

Run once per new edition of the PDF, from a checkout (needs pdftotext, poppler-utils):
    python -m engine.knowledge.split_cgnc <cgnc.pdf>

A piece is one part of the text between two of its own headings: a section (I -, A -) everywhere, one poste
(612 Achats consommés...) in the plan of accounts (Titre III), one rubric or poste (61., 612.) in what each account
holds (Titre IV), one term in the terminology. Each keeps its place (Titre › Chapitre/Classe › heading), its PDF
pages and every account code it names. The text is only cleaned: runs of spaces (justified lines) made one, nothing
reworded."""

import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "cgnc"

TOP = re.compile(r"^(PREMIERE PARTIE|TITRE [IVX]+\b.*)$")  # the Titres (the first part has none)
MIDDLE = re.compile(r"^\s*(Chapitre [IVX]+\b.*|CLASSE \d\b.*|Classe \d\b.*)$")
SECTION = re.compile(
    r"^\s{0,8}([IVX]{1,4} ?[-–] .+"  # I - L'objectif d'information
    r"|[A-H] ?[-–/)] ?\S.+"  # A - Pertinence et fiabilité, A/ PRESENTATION, C) Mise en évidence
    r"|\d{2,3}(?:/\d{3})?(?:\.| -)? +[A-ZÉÈ][A-ZÉÈÀÔ’'“”,;()\-. ]{4,}"  # 612. ACHATS, 11 - CAPITAUX (Titre IV)
    r"|\d{2}\. +[A-ZÉ][a-zéèêàôç’' ,\-]{4,}.*"  # 61. Charges d'exploitation (Titre III's plan)
    r"|\d - [A-ZÉ].+)$"  # 3 - Tableau de financement consolidé
)
POSTE = re.compile(r"^\s{0,8}\d{3}(?:/\d{3})? +[A-ZÉ][a-zéèêàôç’' ,\-]{3,}.*$")  # 612 Achats consommés (Titre III)
TERM = re.compile(r"^- ([A-ZÉÈÀ][A-ZÉÈÀÔ’'(). ,/-]{2,})$")  # - ACTIF CIRCULANT (HORS TRESORERIE), in the terminology
CODE = re.compile(r"(?<![\d,.])(\d{3,6})(?:/(\d{3}))?(?![\d,])")  # 6125, 613/614; not 1.000,00 nor 2 000
ACCOUNT = re.compile(r"^\s*(\d{2,6}(?:/\d{2,3})?)\.? +(\S.*?)\s*$")  # 6125 Achats non stockés..., in the plan
SMALL, LARGE = 200, 5000  # a piece shorter (a bare heading) is joined to the next; longer, cut at blank lines


def text_of(pdf):
    """Return the PDF's pages (pdftotext -layout), each a list of lines."""
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    return [page.split("\n") for page in out.split("\f")]


def cleaned(line):
    """Return ``line`` with its runs of spaces made one, its indentation kept (it shows the plan's levels)."""
    stripped = line.lstrip()
    return line[: len(line) - len(stripped)] + re.sub(r"\s{2,}", " ", stripped.rstrip())


def codes_in(text):
    """Return every account code ``text`` names, in order, once each ("613/614" names 613 and 614).

    A year (1992) or an amount is taken too when it looks like one; codes are only used to find pieces, and a
    code the plan lacks finds nothing."""
    found = []
    for code, other in CODE.findall(text):
        for one in (code, other):
            if one and one[0] != "0" and one not in found:
                found.append(one)
    return found


def split(pages):
    """Return the pieces: [{id, place, title, page_start, page_end, codes, body}] in the CGNC's order."""
    pieces, top, middle, terms, unfinished = [], "", "", False, False
    current = {"title": "", "place": "", "parent": "", "lines": [], "pages": []}

    def close():
        body = "\n".join(current["lines"]).strip("\n")
        if body.strip():
            pieces.append({**current, "body": body})

    def start(title):
        nonlocal current
        close()
        parent = " › ".join(part for part in (top, middle) if part)
        place = " › ".join(part for part in (parent, title) if part)
        current = {"title": title, "place": place, "parent": parent, "lines": [], "pages": []}

    for number, lines in enumerate(pages, 1):
        for raw in lines:
            line = cleaned(raw)
            heading = line.strip()
            if unfinished and heading:  # TITRE IV's title goes on to the next line
                top, unfinished = f"{top} {heading}", False
                current["place"] = current["parent"] = top
            elif TOP.match(heading):
                top, middle, unfinished = heading, "", heading.endswith(" DES")
                start("")
            elif MIDDLE.match(line):
                middle, terms = heading, heading.startswith("Chapitre V : Terminologie")
                start("")
            elif (
                (terms and TERM.match(heading))
                or (not terms and SECTION.match(line))
                or (top.startswith("TITRE III") and POSTE.match(line))
            ):
                start(heading)
            current["lines"].append(line)
            current["pages"].append(number)
    close()
    return numbered(cut(joined(pieces)))


def joined(pieces):
    """Return ``pieces`` with each one shorter than SMALL (a bare heading) joined to the next of the same Titre and
    Chapitre, under the next one's title when it has one: the heading stays above it in its body. Bare headings in a
    row are a list (a Classe's rubrics: 61., 63., 65., 67.): what follows them keeps the first one's title."""
    out = []
    for piece in pieces:
        before = out[-1] if out else None
        if before and len(before["body"]) < SMALL and before["parent"] == piece["parent"]:
            listing = before.get("listing") or len(piece["body"]) < SMALL
            own = piece["title"] and not listing
            out[-1] = {
                **before,
                "lines": before["lines"] + piece["lines"],
                "pages": before["pages"] + piece["pages"],
                "body": before["body"] + "\n" + piece["body"],
                "title": piece["title"] if own else before["title"],
                "place": piece["place"] if own else before["place"],
                "listing": listing,
            }
        else:
            out.append(piece)
    return out


def cut(pieces):
    """Return ``pieces`` with each one longer than LARGE cut at blank lines into parts of about LARGE / 2, each
    titled "(suite)" after the first."""
    out = []
    for piece in pieces:
        if len(piece["body"]) <= LARGE:
            out.append(piece)
            continue
        part, size, first = {"lines": [], "pages": []}, 0, True
        for line, page in zip(piece["lines"], piece["pages"], strict=True):
            if size > LARGE / 2 and not line.strip():
                out.append(part_of(piece, part, first))
                part, size, first = {"lines": [], "pages": []}, 0, False
            part["lines"].append(line)
            part["pages"].append(page)
            size += len(line) + 1
        out.append(part_of(piece, part, first))
    return [piece for piece in out if piece["body"].strip()]


def part_of(piece, part, first):
    suite = "" if first else " (suite)"
    return {
        **piece,
        **part,
        "body": "\n".join(part["lines"]).strip("\n"),
        "title": piece["title"] + suite,
        "place": piece["place"] + suite,
    }


def numbered(pieces):
    """Return the pieces as written to pieces.jsonl, numbered in order, each with its pages and codes."""
    return [
        {
            "id": number,
            "place": piece["place"],
            "title": piece["title"],
            "page_start": min(piece["pages"]),
            "page_end": max(piece["pages"]),
            "codes": codes_in(piece["title"] + "\n" + piece["body"]),
            "body": piece["body"],
        }
        for number, piece in enumerate(pieces, 1)
    ]


def accounts(pages):
    """Return the chart of accounts of the modèle normal (Titre III, « I - Plan des comptes : Modèle normal »):
    [{code, label, parent}], parent the longest shorter code above it (none for a rubric, 2 digits). "613/614"
    gives 613 and 614. A code listed twice keeps its first label."""
    lines = [cleaned(line) for page in pages for line in page]
    begin = next(i for i, line in enumerate(lines) if "Plan des comptes : Modèle normal" in line)
    end = next(i for i, line in enumerate(lines) if "Plan des comptes : Modèle simplifié" in line)
    found = {}
    for line in lines[begin:end]:
        match = ACCOUNT.match(line)
        if not match or MIDDLE.match(line):
            continue
        written, label = match.groups()
        for code in written.split("/"):
            label_seen = label.rstrip(" .")
            if label_seen and code not in found:
                found[code] = label_seen
    rows = []
    for code, label in found.items():
        parent = next((code[:n] for n in range(len(code) - 1, 1, -1) if code[:n] in found), None)
        rows.append({"code": code, "label": label, "parent": parent})
    return rows


def write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def main(pdf):
    pages = text_of(pdf)
    OUT.mkdir(exist_ok=True)
    pieces, plan = split(pages), accounts(pages)
    write(OUT / "pieces.jsonl", pieces)
    write(OUT / "accounts.jsonl", plan)
    print(f"{len(pieces)} pieces, {len(plan)} accounts, in {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
