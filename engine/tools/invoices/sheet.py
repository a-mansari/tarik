"""A spreadsheet of invoices (Experio's template canvas_factures.xlsx, or a client's own table) imported as draft
invoices through Experio's own import screen for exp.invoice (base_import.import), as « Importer » does in the invoice
list.

- Columns: Experio's screen maps its template's headers itself; a free-form header is mapped by FREE_FORM when usual,
  else asked, with the headers Tarik could not place and what the invoice still needs. The client's answer comes back
  as ``columns``. The columns used are always told, so a wrong guess is corrected.
- Side: the sheet has none; the client says purchase or sale (``invoice_type``), else it is asked.
- Duplicates: an invoice with a number and tiers already in the dossier (or twice in the sheet) is refused the first
  time, with the list; the client who insists (``keep_duplicates``) has them imported and told which were twins.
- Test, then import: Experio's test (every row checked, e.g. a tiers it does not know) first; its refusals are told
  row by row and nothing is imported. The drafts made have no scan: the run goes on from classify_invoice.
"""

import csv
import io

from engine.tools.core.app import ok, question, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import dry_run, in_dossier, odoo_call, require_dossier
from engine.tools.core.words import as_number, counted, normalized
from engine.tools.dossiers.setup import (
    IMPORT_OPTIONS,
    Refused,
    imported,
    previewed,
    separator_of,
    table_text,
    taken,
    tested,
)
from engine.tools.invoices.classify import CATEGORY_TO_TYPE
from engine.tools.invoices.intake import file_row
from engine.tools.invoices.sheet_headers import FIELD_LABELS, FREE_FORM, REQUIRED, TIERS, TOTALS

LABEL = "Tableur de factures"
SIDES = {"achat": "purchase", "purchase": "purchase", "vente": "sale", "sale": "sale", **CATEGORY_TO_TYPE}


def side_of(invoice_type):
    """Return "purchase" or "sale" from the client's word (achat, vente), None when it says neither."""
    return SIDES.get(normalized(invoice_type or ""))


def parsed_columns(columns):
    """Return {normalized header: Experio field, or False to leave the column out} from the client's ``columns``: one
    « en-tête = champ » per line or per semicolon (« ignorer » or nothing after the = leaves a column out)."""
    wanted = {}
    for part in (columns or "").replace(";", "\n").splitlines():
        header, _, field = part.partition("=")
        if header.strip():
            field = field.strip()
            wanted[normalized(header)] = False if normalized(field) in ("", "ignorer") else field
    return wanted


def mapped_fields(preview, wanted):
    """Return the Experio field of each column of ``preview`` (False: left out): the client's choice, else the
    import screen's best suggestion, else FREE_FORM's guess; a field is given to one column only."""
    fields = []
    for i, header in enumerate(preview["headers"]):
        suggested = (preview["matches"].get(str(i)) or [False])[0]
        key = normalized(header)
        fields.append(wanted[key] if key in wanted else suggested or FREE_FORM.get(key, False))
    for i, field in enumerate(fields):  # a field given twice stays on its first column
        if field and field in fields[:i]:
            fields[i] = False
    return fields


def missing(fields):
    """Return the labels of what an invoice still needs that no column in ``fields`` gives."""
    lacks = [FIELD_LABELS[f] for f in REQUIRED if f not in fields]
    lacks = ([FIELD_LABELS[TIERS[0]]] if not set(TIERS) & set(fields) else []) + lacks
    return lacks + (
        [f"{FIELD_LABELS[TOTALS[0]]} ou {FIELD_LABELS[TOTALS[1]]}"] if not set(TOTALS) & set(fields) else []
    )


def rows_of(text, separator, fields):
    """Return the rows of ``text`` as [{Experio field: cell}], only the columns mapped."""
    table = list(csv.reader(io.StringIO(text), delimiter=separator))[1:]
    return [{f: row[i].strip() for i, f in enumerate(fields) if f and i < len(row)} for row in table if any(row)]


def cell_number(cell):
    """Return the cell's number, None when it is empty or not clearly one."""
    try:
        return as_number(cell) if cell.strip() else None
    except ValueError:
        return None


TAX_FIELDS = {"amount_tva", "multi_tva", "amount_tax_non_recoverable"}


def with_vat(text, separator, fields):
    """Return ``text`` with a last column « TVA calculée » (TTC minus HT, to the centime) when the sheet gives the HT
    and the TTC but no TVA at all (Experio refuses an invoice whose HT, TVA and other amounts do not add up to its
    TTC); None when nothing is added. A row missing either amount, or with a TTC under its HT, gets no value."""
    taxed = {f for f in fields if f and (f in TAX_FIELDS or f.startswith("amount_tva_"))}
    if taxed or not set(TOTALS) <= set(fields):
        return None
    ht, ttc = fields.index(TOTALS[0]), fields.index(TOTALS[1])
    table = list(csv.reader(io.StringIO(text), delimiter=separator))
    for number, row in enumerate(table):
        if number == 0:
            row.append("TVA calculée")
            continue
        low, high = (cell_number(row[i]) if i < len(row) else None for i in (ht, ttc))
        row += [""] * (len(table[0]) - 1 - len(row))
        row.append(f"{high - low:.2f}" if low is not None and high is not None and high >= low else "")
    out = io.StringIO()
    csv.writer(out, delimiter=separator, lineterminator="\n").writerows(table)
    return out.getvalue()


def twins(rows, side, dossier_id, context):
    """Return the lines « n° X (tiers) » of the rows whose number and tiers the dossier already holds as an invoice
    of this side (cancelled ones left out), or that the sheet holds twice."""
    numbers = sorted({r["invoice_number"] for r in rows if r.get("invoice_number")})
    found = odoo_call(
        "exp.invoice",
        "search_read",
        [
            ("company_id", "=", dossier_id),
            ("type", "=", side),
            ("state", "!=", "cancel"),
            ("invoice_number", "in", numbers),
        ],
        fields=["invoice_number", "partner_id"],
        context=context,
    )
    # Experio names a tiers « NOM - compte - ICE »: a sheet's name matches when it starts that name.
    held = [
        (normalized(i["invoice_number"] or ""), normalized(i["partner_id"][1] if i["partner_id"] else ""))
        for i in found
    ]
    seen, lines = set(), []
    for r in rows:
        who = r.get("partner_id") or r.get("partner_name", "")
        number, name = normalized(r.get("invoice_number", "")), normalized(who)
        if not number:
            continue
        if (number, name) in seen or any(n == number and h.startswith(name) for n, h in held):
            lines.append(f"n° {r['invoice_number']} ({who or '?'})")
        seen.add((number, name))
    return lines


@step_tool
def import_invoice_sheet(
    dossier_id: int, file_id: int, invoice_type: str = "", columns: str = "", keep_duplicates: bool = False
) -> dict:
    """Import a spreadsheet of invoices (Experio's template or the client's own table; .xlsx, .xlsm, .csv, .txt) as
    draft invoices into the dossier, through Experio's own invoice import.

    ``invoice_type``: achat or vente, as the client said; asked when empty.
    ``columns``: the client's answer about columns Tarik could not place: « en-tête = champ » per line, the field being
    one named in the question (« ignorer » leaves a column out).
    ``keep_duplicates``: true only when the client insists on importing the invoices the dossier already holds.

    Returns:
        - a question result (the file stays) when the side, a column or the duplicates need the client's word;
        - a stop result when Experio's test or import refuses a row (nothing imported, file taken);
        - an ok result with ``invoice_ids``, the drafts made, for the run to take on from classify_invoice.
          In test mode, only Experio's test runs.
    """
    dossier = require_dossier(dossier_id)
    with cabinet_db() as db:
        name, mime, data = file_row(db, file_id, "file_name", "mime_type", "data")
    side = side_of(invoice_type)
    if not side:
        return question(f"`{name}` : ces factures sont-elles des factures d'achat ou de vente ?", file_id=file_id)
    context = {**in_dossier(dossier_id), "default_type": side, "import_source_document": "import"}
    try:
        text = table_text(name, data)
        import_id, preview = previewed(file_id, name, text, context, "exp.invoice")
        fields = mapped_fields(preview, parsed_columns(columns))
        lacks = missing(fields)
        if lacks:
            left = ", ".join(f"`{h}`" for h, f in zip(preview["headers"], fields, strict=True) if not f)
            offered = ", ".join(f"`{f}` ({label})" for f, label in FIELD_LABELS.items())
            return question(
                f"`{name}` : je n'ai pas pu placer toutes les colonnes. Il me manque {', '.join(lacks)}. "
                f"Colonnes non reconnues : {left or 'aucune'}. Pour chacune, dites-moi ce qu'elle contient parmi : "
                f"{offered}, ou « ignorer ».",
                choices=FIELD_LABELS,
                file_id=file_id,
            )
        separator = preview.get("options", {}).get("separator") or separator_of(text)
        computed = with_vat(text, separator, fields)
        if computed:  # the client's HT and TTC, no TVA: the difference is the TVA, told with the columns
            text = computed
            import_id, preview = previewed(file_id, name, text, context, "exp.invoice")
            fields = mapped_fields(preview, {**parsed_columns(columns), "tva calculee": "amount_tva"})
        rows = rows_of(text, separator, fields)
        same = twins(rows, side, dossier_id, context)
        if same and not keep_duplicates:
            return question(
                f"`{name}` : {counted(len(same), '{} facture existe', '{} factures existent')} déjà dans le dossier "
                f"`{dossier}` (ou deux fois dans le fichier) : {', '.join(same[:10])}. Je n'ai rien importé. Dites-moi "
                "de les importer quand même si vous le voulez.",
                file_id=file_id,
            )
        options = {**IMPORT_OPTIONS, **preview.get("options", {})}
        arguments = ([import_id], fields, preview["headers"], options)
        test = tested(file_id, name, LABEL, arguments, context)
        used = ("TVA calculée (TTC moins HT) ; " if computed else "") + ", ".join(
            f"`{h}` → {FIELD_LABELS.get(f, f)}" for h, f in zip(preview["headers"], fields, strict=True) if f
        )
        if dry_run():
            made = counted(len(test.get("ids") or []), "{} facture", "{} factures")
            return ok(
                f"{TEST_MODE}{LABEL} `{name}` : le test d'Experio a réussi sur {made}, je n'ai rien importé.",
                file_id=file_id,
            )
        result = imported(file_id, name, LABEL, arguments, context)
    except Refused as refusal:
        taken(file_id)
        return stop(str(refusal), file_id=file_id)
    taken(file_id)
    ids = result.get("ids") or []
    note = f" Déjà présentes, importées à votre demande : {', '.join(same[:10])}." if same else ""
    return ok(
        f"{LABEL} `{name}` : {counted(len(ids), '{} brouillon créé', '{} brouillons créés')} dans le dossier "
        f"`{dossier}`. Colonnes lues : {used}.{note}",
        file_id=file_id,
        invoice_ids=ids,
    )
