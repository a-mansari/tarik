"""A routine's report on many dossiers: a few lines for the email's body, every dossier in the files joined to it
(Excel, CSV for any other tool, text to read as is). Each dossier is a row: its name, its result, and for the
progress Experio's figures (dossier_progress_figures), then the dossier's whole text."""

import csv
import io

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font

READ, PREPARED, ALREADY, NOTHING_TO_DO, TO_SEE, FAILED, LEFT = (
    "Lu",
    "Préparée",
    "Déjà faite",
    "Rien à préparer",
    "À voir",
    "Échec",
    "Non traité",
)
FIGURES = [  # the progress's columns: figure key, its header
    ("progress", "Avancement (%)"),
    ("pieces_booked", "Pièces comptabilisées"),
    ("pieces", "Pièces"),
    ("operations_booked", "Opérations comptabilisées"),
    ("operations", "Opérations"),
    ("suspense", "Opérations en suspens (comptabilisées, non lettrées)"),
    ("exercice", "Exercice"),
]
NAMED = 5  # dossiers named in the email's body, the rest only counted
FORMULA = ("=", "+", "-", "@", "\t", "\r")  # a cell starting so is run by Excel as a formula: kept as text
CELL_CHARACTERS = 32_000  # Excel holds at most 32 767 characters in a cell: the rest is in the text file


def cell(value):
    """Return ``value`` safe in a spreadsheet: control characters Excel refuses dropped, cut to CELL_CHARACTERS, and
    a text Excel would run as a formula (a dossier named « =HYPERLINK(...) ») kept as text with a leading quote."""
    if not isinstance(value, str):
        return value
    value = ILLEGAL_CHARACTERS_RE.sub("", value)[:CELL_CHARACTERS]
    return "'" + value if value.startswith(FORMULA) else value


def code(name):
    """Return ``name`` between backticks in Tarik's Markdown, its own backticks made quotes: never ends the span."""
    return "`" + name.replace("`", "'") + "`"


def table(task, parts):
    """Return (header, rows) of the report's table: one row per dossier part ({dossier, result, detail, figures?}),
    the progress's figures in their own columns."""
    figures = FIGURES if task == "progress" else []
    header = ["Dossier", "Résultat", *(words for _, words in figures), "Détail"]
    rows = [
        [part["dossier"], part["result"], *((part.get("figures") or {}).get(key) for key, _ in figures), part["detail"]]
        for part in parts
    ]
    rows = [[cell(value) for value in row] for row in rows]
    return header, rows


def workbook(header, rows):
    """Return the table as an Excel file (bytes): its header bold and kept in view, filters on every column."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Dossiers"
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, words in enumerate(header, start=1):
        letter = openpyxl.utils.get_column_letter(index)
        sheet.column_dimensions[letter].width = 80 if words == "Détail" else max(12, len(words) + 2)
        if words == "Détail":
            for cell in sheet[letter][1:]:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    saved = io.BytesIO()
    book.save(saved)
    return saved.getvalue()


def spreadsheet_text(header, rows):
    """Return the table as CSV (bytes): « ; » between fields and a byte order mark, as Excel opens it in French."""
    written = io.StringIO()
    out = csv.writer(written, delimiter=";")
    out.writerow(header)
    out.writerows([["" if value is None else value for value in row] for row in rows])
    return written.getvalue().encode("utf-8-sig")


def plain(parts):
    """Return every dossier's whole text, one after the other: the report to read as is."""
    texts = []
    for part in parts:
        named_already = part["detail"].startswith("Dossier")  # the progress's text, a declaration's own summary
        texts.append(part["detail"] if named_already else f"Dossier `{part['dossier']}` : {part['detail']}")
    return "\n\n".join(texts)


def plural(count, one, many=None):
    """Return « 1 dossier », « 3 dossiers »: ``count`` in bold, then its word in the singular or the plural."""
    return f"**{count}** {one if count == 1 else many or one + 's'}"


def listed(parts, result):
    """Return the Markdown list of the dossiers with ``result``, the first NAMED, then how many more."""
    names = [part["dossier"] for part in parts if part["result"] == result]
    lines = [f"- {code(name)}" for name in names[:NAMED]]
    if len(names) > NAMED:
        lines.append(f"- et {plural(len(names) - NAMED, 'autre')} dans les fichiers joints")
    return lines


def summary(task, parts, title, files):
    """Return the email's body in Tarik's Markdown (send_email renders it): ``title`` (the routine and its task),
    then in short how many dossiers and how each ended; for the progress Experio's mean progress, the bank
    operations in suspense and the least advanced; the dossiers to see and those failed; the ``files`` joined, where
    every dossier is."""
    words = {
        READ: ("lu", "lus"),
        PREPARED: ("préparée", "préparées"),
        ALREADY: ("déjà faite", "déjà faites"),
        NOTHING_TO_DO: ("sans rien à préparer", "sans rien à préparer"),
        TO_SEE: ("à voir", "à voir"),
        FAILED: ("en échec", "en échec"),
        LEFT: ("non traité", "non traités"),
    }
    counts = {result: sum(part["result"] == result for part in parts) for result in words}
    ended = ", ".join(f"**{n}** {words[r][n != 1]}" for r, n in counts.items() if n)
    if counts[LEFT]:
        ended += " (la période suivante est arrivée avant)"
    lines = ["Bonjour,", "", f"Voici le rapport de la {title}.", "", "**En bref**", ""]
    lines.append(f"- {plural(len(parts), 'dossier')} parcouru{'s' if len(parts) != 1 else ''} : {ended}")
    least = []
    if task == "progress":
        read = [part for part in parts if part["result"] == READ and part["figures"]["progress"] is not None]
        if read:
            mean = round(sum(part["figures"]["progress"] for part in read) / len(read))
            lines.append(f"- Avancement moyen des dossiers (celui de chacun selon Experio) : **{mean} %**")
            behind = [part for part in read if part["figures"]["progress"] < 100]  # done ones are never « behind »
            least = sorted(behind, key=lambda part: part["figures"]["progress"])[:NAMED]
        waiting = [part for part in parts if part["result"] == READ and part["figures"]["suspense"]]
        if waiting:
            total = sum(part["figures"]["suspense"] for part in waiting)
            dossiers = plural(len(waiting), "dossier")
            suspense = "Opérations bancaires en suspens (comptabilisées, sans facture lettrée, exercice en cours)"
            lines.append(f"- {suspense} : {plural(total, 'opération')} dans {dossiers}")
    if least:
        lines += ["", "**Les moins avancés**", ""]
        lines += [f"{i}. {code(part['dossier'])} : {part['figures']['progress']} %" for i, part in enumerate(least, 1)]
    for result, heading in ((TO_SEE, "À voir"), (FAILED, "En échec")):
        if counts[result]:
            lines += ["", f"**{heading}**", "", *listed(parts, result)]
    excel, spreadsheet, text = files
    lines += [
        "",
        "**Le détail de chaque dossier est joint**",
        "",
        f"- `{excel}` : le tableau, un dossier par ligne, avec des filtres",
        f"- `{spreadsheet}` : le même tableau, pour un autre logiciel",
        f"- `{text}` : le détail complet, à lire tel quel",
    ]
    return "\n".join(lines)
