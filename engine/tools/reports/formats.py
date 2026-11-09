"""The formats a Document (document.py) is written in: Markdown, Word, Excel, PDF and LaTeX source, each from the same
blocks and each escaping text its own way. A renderer takes the Document and returns (the file's bytes, notes): the
notes are what the reader should know (characters a format could not hold).

PDF uses the standard Helvetica font, so a character outside Windows-1252 (Arabic, say) is written as "?" and noted;
LaTeX is the source only, never compiled here, so nothing a client typed can run."""

import io
import re
from xml.sax.saxutils import escape as xml_escape

import openpyxl
from docx import Document as WordDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.doctemplate import LayoutError

from engine.tools.core.app import ToolError

MAX_BYTES = 8 * 1024 * 1024  # one file, so that the reply that carries them stays within what a mail takes


def number_of(text):
    """Return ``text`` as the number it writes (« 1 234,56 », « -12.5 », « 7 »), else None: ASCII digits only (an
    Arabic-Indic figure stays the text it is), and a leading zero (a code) or more than 11 digits (an account, a
    phone, an ICE: Excel would show 1.23E+13) stay text. A comma is a decimal one, as in French: « 1,234 » is 1.234."""
    compact = text.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if not re.fullmatch(r"-?[0-9]+([.,][0-9]+)?", compact) or re.fullmatch(r"-?0[0-9]+([.,][0-9]+)?", compact):
        return None
    if len(re.sub(r"[^0-9]", "", compact)) > 11:
        return None
    value = float(compact.replace(",", "."))
    return int(value) if value.is_integer() and "," not in compact and "." not in compact else value


# --- Markdown ---


def md_text(text):
    """Return ``text`` safe in a Markdown file: no tag nor link a viewer would render from what was typed."""
    return text.replace("<", "&lt;").replace("[", "\\[")


def md_line(text):
    """Return ``text`` on one line (a heading, a title, an item), md_text'd."""
    return md_text(text).replace("\n", " ")


def md_cell(text):
    return md_line(text).replace("|", "\\|")


def render_md(document):
    lines = [f"# {md_line(document.title)}", ""]
    if document.subtitle:
        lines += [f"*{md_line(document.subtitle)}*", ""]
    for block in document.blocks:
        kind = block["type"]
        if kind == "heading":
            lines += ["#" * (block["level"] + 1) + " " + md_line(block["text"]), ""]
        elif kind == "paragraph":
            lines += [md_text(block["text"]), ""]
        elif kind == "note":
            lines += [f"> {line}" for line in md_text(block["text"]).splitlines()] + [""]
        elif kind == "bullets":
            lines += [f"- {md_line(item)}" for item in block["items"]] + [""]
        elif kind == "numbered":
            lines += [f"{n}. {md_line(item)}" for n, item in enumerate(block["items"], 1)] + [""]
        else:
            if block["caption"]:
                lines += [f"**{md_line(block['caption'])}**", ""]
            lines.append("| " + " | ".join(md_cell(h) for h in block["headers"]) + " |")
            lines.append("| " + " | ".join("---" for _ in block["headers"]) + " |")
            lines += ["| " + " | ".join(md_cell(c) for c in row) + " |" for row in block["rows"]]
            lines.append("")
    return "\n".join(lines).encode("utf-8"), []


# --- Word ---


def render_docx(document):
    word = WordDocument()
    word.core_properties.title, word.core_properties.author = document.title, "Tarik"
    for section in word.sections:
        section.left_margin = section.right_margin = Cm(2.2)
    word.add_heading(document.title, level=0)
    if document.subtitle:
        word.add_paragraph(document.subtitle, style="Subtitle")
    for block in document.blocks:
        kind = block["type"]
        if kind == "heading":
            word.add_heading(block["text"], level=block["level"])
        elif kind == "paragraph":
            word.add_paragraph(block["text"])
        elif kind == "note":
            paragraph = word.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.6)
            paragraph.add_run(block["text"]).italic = True
        elif kind == "bullets":
            for item in block["items"]:
                word.add_paragraph(item, style="List Bullet")
        elif kind == "numbered":  # written out: Word's own numbering would run on from one list to the next
            for n, item in enumerate(block["items"], 1):
                paragraph = word.add_paragraph(f"{n}. {item}")
                paragraph.paragraph_format.left_indent = Cm(0.6)
        else:
            if block["caption"]:
                word.add_paragraph(block["caption"], style="Caption")
            table = word.add_table(rows=1, cols=len(block["headers"]))
            table.style = "Table Grid"
            for cell, header in zip(table.rows[0].cells, block["headers"], strict=True):
                cell.paragraphs[0].add_run(header).bold = True
            for row in block["rows"]:
                for cell, text in zip(table.add_row().cells, row, strict=True):
                    cell.text = text
                    if number_of(text) is not None:
                        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(9)
            word.add_paragraph()
    out = io.BytesIO()
    word.save(out)
    return out.getvalue(), []


# --- Excel ---

SHEET_NAME = re.compile(r"[\[\]:*?/\\]")
HEADER_FILL = PatternFill("solid", fgColor="DDE7EE")


def sheet_name(wanted, taken):
    """Return a sheet name from ``wanted``: no character Excel forbids, no apostrophe at its ends, not « History »,
    at most 31 long, not among ``taken``."""
    base = re.sub(r"\s+", " ", SHEET_NAME.sub(" ", wanted)).strip().strip("'").strip()[:28] or "Tableau"
    name, n = base, 2
    while name.lower() in {t.lower() for t in taken} | {"history"}:  # « History » is Excel's own
        name, n = f"{base[:26]} {n}", n + 1
    return name


def put(sheet, row, column, text, **style):
    """Write ``text`` at (row, column): a number when it is one, else a string Excel never reads as a formula."""
    cell = sheet.cell(row=row, column=column)
    number = number_of(text)
    if number is not None:
        cell.value = number
        cell.alignment = Alignment(horizontal="right")
    else:
        cell.value = text
        cell.data_type = "s"  # « =1+1 », « +cmd », « @SUM »: text, not a formula
    for name, value in style.items():
        setattr(cell, name, value)
    return cell


def render_xlsx(document):
    book = openpyxl.Workbook()
    main = book.active
    main.title = "Rapport"
    taken, row = ["Rapport"], 1

    def line(text, gap=0, **style):
        """Write ``text`` on the main sheet's next row, then leave ``gap`` empty rows."""
        nonlocal row
        put(main, row, 1, text, **style)
        row += 1 + gap

    line(document.title, font=Font(bold=True, size=14))
    if document.subtitle:
        line(document.subtitle, font=Font(italic=True))
    row += 1
    for block in document.blocks:
        kind = block["type"]
        if kind == "table":
            name = sheet_name(block["caption"] or "Tableau", taken)
            taken.append(name)
            sheet = book.create_sheet(name)
            for column, header in enumerate(block["headers"], 1):
                put(sheet, 1, column, header, font=Font(bold=True), fill=HEADER_FILL)
            for r, cells in enumerate(block["rows"], 2):
                for column, text in enumerate(cells, 1):
                    put(sheet, r, column, text)
            for column in range(len(block["headers"])):
                texts = [block["headers"][column]] + [cells[column] for cells in block["rows"][:200]]
                letter = openpyxl.utils.get_column_letter(column + 1)
                sheet.column_dimensions[letter].width = min(60, max(8, max(len(t) for t in texts) + 2))
            sheet.freeze_panes = "A2"
            line(f"Tableau : onglet « {name} »", font=Font(italic=True))
        elif kind == "heading":
            line(block["text"], gap=1, font=Font(bold=True, size=13 - block["level"]))
        elif kind == "paragraph":
            line(block["text"], alignment=Alignment(wrap_text=True, vertical="top"))
        elif kind == "note":
            line(block["text"], alignment=Alignment(wrap_text=True, vertical="top"), font=Font(italic=True))
        else:
            for n, item in enumerate(block["items"], 1):
                line(f"{'•' if kind == 'bullets' else str(n) + '.'} {item}")
    main.column_dimensions["A"].width = 100
    out = io.BytesIO()
    book.save(out)
    return out.getvalue(), []


# --- PDF ---


class Latin:
    """Text for the PDF's standard font: what Windows-1252 holds, "?" for the rest (counted in ``lost``)."""

    def __init__(self):
        self.lost = 0

    def __call__(self, text):
        out = []
        for char in text.replace("\u202f", " "):
            try:
                char.encode("cp1252")
                out.append(char)
            except UnicodeEncodeError:
                out.append("?")
                self.lost += 1
        return "".join(out)


def paragraph(latin, text, style):
    return Paragraph(xml_escape(latin(text)).replace("\n", "<br/>"), style)


def render_pdf(document):
    latin = Latin()
    sheet = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=sheet["BodyText"], fontSize=10, leading=14)
    note = ParagraphStyle(
        "note", parent=body, textColor=colors.HexColor("#444444"), leftIndent=12, fontName="Helvetica-Oblique"
    )
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10)
    bold = ParagraphStyle("th", parent=small, fontName="Helvetica-Bold")
    item_style = ParagraphStyle("li", parent=body, leftIndent=16, firstLineIndent=-10)
    head = {1: sheet["Heading1"], 2: sheet["Heading2"], 3: sheet["Heading3"]}
    story = [paragraph(latin, document.title, sheet["Title"])]
    if document.subtitle:
        story.append(paragraph(latin, document.subtitle, sheet["Italic"]))
    width = A4[0] - 4 * cm
    for block in document.blocks:
        kind = block["type"]
        if kind == "heading":
            story.append(paragraph(latin, block["text"], head[block["level"]]))
        elif kind == "paragraph":
            story.append(paragraph(latin, block["text"], body))
        elif kind == "note":
            story.append(paragraph(latin, block["text"], note))
        elif kind in ("bullets", "numbered"):
            for n, item in enumerate(block["items"], 1):
                mark = "\u2022" if kind == "bullets" else f"{n}."
                story.append(paragraph(latin, f"{mark} {item}", item_style))
        else:
            if block["caption"]:
                story.append(paragraph(latin, block["caption"], sheet["Heading4"]))
            data = [[paragraph(latin, cell, bold) for cell in block["headers"]]]
            data += [[paragraph(latin, cell, small) for cell in row] for row in block["rows"]]
            table = Table(data, colWidths=[width / len(block["headers"])] * len(block["headers"]), repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE7EE")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story += [table, Spacer(1, 8)]

    def page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(2 * cm, 1.2 * cm, latin(document.title)[:90])
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    pdf = SimpleDocTemplate(
        out, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm
    )
    pdf.title, pdf.author = latin(document.title), "Tarik"
    try:
        pdf.build(story, onFirstPage=page, onLaterPages=page)
    except LayoutError:  # a table row taller than a page: reportlab cannot split a row
        raise ToolError(
            "Un tableau est trop dense pour le PDF (une ligne plus haute qu'une page) : moins de colonnes ou des "
            "cellules plus courtes."
        ) from None
    notes = [f"{latin.lost} caractère(s) hors du jeu Latin remplacé(s) par « ? » dans le PDF."] if latin.lost else []
    return out.getvalue(), notes


# --- LaTeX ---

TEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "^": r"\textasciicircum{}",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
}
TEX_HEAD = r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[french]{babel}
\usepackage[margin=2.5cm]{geometry}
\usepackage{longtable,booktabs,array}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.6em}
"""


def tex(text):
    """Return ``text`` with every character LaTeX reads as a command written as plain text; a blank line stays one."""
    return "\n\n".join(
        "".join(TEX_ESCAPES.get(char, char) for char in part.replace("\n", " ")) for part in text.split("\n\n")
    )


def render_tex(document):
    out = [
        TEX_HEAD,
        f"\\title{{{tex(document.title)}}}",
        "\\author{Tarik}",
        "\\date{}",
        r"\begin{document}",
        r"\maketitle",
    ]
    if document.subtitle:
        out.append(f"\\begin{{center}}\\emph{{{tex(document.subtitle)}}}\\end{{center}}")
    for block in document.blocks:
        kind = block["type"]
        if kind == "heading":
            out.append(f"\\{'sub' * (block['level'] - 1)}section{{{tex(block['text'])}}}")
        elif kind == "paragraph":
            out.append(tex(block["text"]))
        elif kind == "note":
            out.append(f"\\begin{{quote}}\\emph{{{tex(block['text'])}}}\\end{{quote}}")
        elif kind in ("bullets", "numbered"):
            env = "itemize" if kind == "bullets" else "enumerate"
            out += [f"\\begin{{{env}}}", *[f"\\item {{}}{tex(item)}" for item in block["items"]], f"\\end{{{env}}}"]
        else:
            n = len(block["headers"])
            if block["caption"]:
                out.append(f"\\noindent\\textbf{{{tex(block['caption'])}}}\\par\\smallskip")
            column = r">{\raggedright\arraybackslash}p{\dimexpr(\linewidth-" + f"{2 * n}\\tabcolsep)/{n}\\relax}}"
            out.append(f"\\begin{{longtable}}{{@{{}}{column * n}@{{}}}}")
            out.append(r"\toprule")
            out.append(" & ".join(f"\\textbf{{{tex(h)}}}" for h in block["headers"]) + r" \\ \midrule \endhead")
            out += [
                "{} " + " & ".join(tex(cell) for cell in row) + r" \\" for row in block["rows"]
            ]  # « {} »: a « [ » is no option
            out += [r"\bottomrule", r"\end{longtable}"]
    out.append(r"\end{document}")
    return "\n".join(out).encode("utf-8"), ["Fichier source LaTeX : à compiler pour obtenir un PDF."]


RENDERERS = {"md": render_md, "docx": render_docx, "xlsx": render_xlsx, "pdf": render_pdf, "tex": render_tex}
