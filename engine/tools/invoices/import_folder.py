"""Make the client's import documents into an Experio import folder (exp.import.folder), as Experio's own « Imports »
screen does (form_with_drop_area.js _importFolder, wizard exp.folder.import).

An import folder (dossier d'import) holds one import's documents: the transit agent's invoice, customs, transport,
storage, the foreign supplier's invoice, the DUM. Experio's screen types each page (Transit, Douane, DUM, Pièce
jointe...), then:
- imports each page that is a document (not a DUM nor an attachment) through exp.invoice.import with its type, its
  reading held back (defer_bluejay_send);
- creates the folder from those drafts (create_folder_from_js, which sends their reading);
- sends the DUM pages to be read on their own (bluejay_create_dum_tokens_from_javascript);
- attaches the attachment pages to the folder (action_attach_pages_from_javascript).
Tarik does the same, each page typed by the model among Experio's own types, except that it attaches the DUM unread:
Experio's DUM reading needs the screen's web request (exp_folder_import.py:305). The drafts made then go through the
invoice's usual steps.
Used only on the client's own words: « dossier d'import », and « plusieurs dossiers » for one folder per file."""

import base64
import re

from engine.tools.core import pdfs
from engine.tools.core.app import ToolError, log, ok, question, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import dry_run, in_dossier, odoo_call, odoo_json, refused, require_dossier
from engine.tools.core.model import ask_model, pdf_pages, pdfium, to_pdf, tool_prompt
from engine.tools.core.queries import Q
from engine.tools.core.words import counted, normalized

FOLDER_WORDS = re.compile(r"(?<![a-z])(?:dossiers? d ?import|dossiers? import)(?![a-z])")
MULTIPLE_WORDS = re.compile(
    r"(?<![a-z])(?:plusieurs dossiers|un dossier par fichier|chaque fichier (?:est )?un dossier)"
)
NOT_DOCUMENTS = ("dum", "attachment")  # Experio's EXCLUDED_INVOICE_TYPES: no exp.invoice made of them


def folder_mode(words):
    """Return (wants_folder, one_per_file) from the client's ``words``: whether they ask for an import folder
    (« dossier d'import »), and whether for one folder per file (« plusieurs dossiers », « un dossier par fichier »)."""
    words = normalized(words or "")
    return bool(FOLDER_WORDS.search(words)), bool(MULTIPLE_WORDS.search(words))


SIGNS = ("dum", "transit", "customs")  # pages only an import folder holds


def looks_like_folder(session_id, dossier_id):
    """True when the conversation's new files are an import folder the client did not name: two files or more, and
    the first page of one is a DUM, a transit agent's invoice or a customs document. One quick model call per file's
    first page, stopping at the first sign; False on any failure: each file then goes its usual way."""
    with cabinet_db() as db:
        rows = db.execute(Q.folder_files_of_session, (session_id,)).fetchall()
    rows = [row for row in rows if row[2] == "application/pdf" or row[2].startswith("image/")]
    if len(rows) < 2:
        return False
    try:
        kinds, dossier = page_types(), require_dossier(dossier_id)
        for _, name, mime, data in rows:
            raw = to_pdf(bytes(data)) if mime.startswith("image/") else bytes(data)
            what = f"Dossier d'import ? `{name}` page 1"
            if typed_page(pdf_pages(raw, limit=1)[0], kinds, dossier, what)[0] in SIGNS:
                return True
    except Exception as error:  # a guess only: any failure leaves the files to their usual way
        log(f"looks_like_folder {session_id}: {error!r}")
    return False


def page_types():
    """Return Experio's page types for an import folder as {value: label}, from exp.folder.import's import_invoice_type
    selection. The labels are read in French because they are shown to the client, and Tarik's Experio user may read
    Experio in English."""
    found = odoo_call(
        "exp.folder.import", "fields_get", ["import_invoice_type"], attributes=["selection"], context={"lang": "fr_FR"}
    )
    return dict(found["import_invoice_type"]["selection"])


def typed_page(image, kinds, dossier, what):
    """Ask the model for one page's (type, starts).

    Args:
        image: the page's image.
        kinds: Experio's page types {value: label} (page_types).
        dossier: the dossier's name: the importer.
        what: names the request in the log and in the error when the model fails.

    Returns (type, starts): ``type`` is a key of ``kinds`` ("other" when the model answers anything else); ``starts`` is
    False only when the model says the page continues the previous one, True when it begins a document (its own title,
    number or letterhead)."""
    listed = "\n".join(f"- {value}: {label}" for value, label in kinds.items())
    instruction = (
        f"The cabinet's client here is « {dossier} », the importer.\nExperio's page types:\n{listed}\n\n"
        'Answer with only a JSON object: {"type": "<one value above>", "starts": true|false, '
        '"reason": "one short French sentence"}'
    )
    answer = ask_model(tool_prompt("classify-import-page"), [image], instruction, what, think=False)
    kind = answer.get("type") if answer.get("type") in kinds else "other"
    return kind, answer.get("starts") is not False


def documents_of(raw, kinds, dossier, name):
    """Return the documents of PDF ``raw`` as [(type, [0-based page numbers])]: a new document begins at each page the
    model says starts one or whose type differs from the previous page's. ``kinds``: Experio's page types; ``dossier``:
    the dossier's name; ``name``: the file's name, for the log."""
    images = pdf_pages(raw, limit=None)
    documents = []
    for number, image in enumerate(images):
        kind, starts = typed_page(image, kinds, dossier, f"Dossier d'import : `{name}` page {number + 1}")
        if documents and not starts and documents[-1][0] == kind:
            documents[-1][1].append(number)
        else:
            documents.append((kind, [number]))
    return documents


def pages_pdf(raw, numbers):
    """Return a new PDF (bytes) made of pages ``numbers`` (0-based) of PDF ``raw``."""
    return pdfium(pdfs.picked, raw, list(numbers))


def b64(data):
    return base64.b64encode(data).decode()


def make_folder(files, kinds, dossier, multiple):
    """Cut ``files`` into the documents of each folder to create.

    Args:
        files: [(file_id, name, raw PDF bytes)].
        kinds: Experio's page types (page_types).
        dossier: the dossier's name, for the model.
        multiple: True for one folder per file, False for one folder for all ``files``.

    Returns [(group, documents)], one per folder: ``group`` is that folder's files (as in ``files``); ``documents`` is
    [(file_id, part name, type, PDF bytes)], one per document found in them."""
    groups = [[file] for file in files] if multiple else [files]
    folders = []
    for group in groups:
        documents = []
        for file_id, name, raw in group:
            for kind, numbers in documents_of(raw, kinds, dossier, name):
                part = f"{name.rsplit('.', 1)[0]} - {kinds[kind]} - pages {numbers[0] + 1}-{numbers[-1] + 1}.pdf"
                documents.append((file_id, part, kind, pages_pdf(raw, numbers)))
        folders.append((group, documents))
    return folders


def create_in_experio(dossier_id, dossier, group, documents, number_rule):
    """Create one import folder in Experio the way its « Imports » screen does; return (folder_id, made).

    Args:
        dossier_id: the dossier.
        dossier: its name, for the model reading the DUM.
        group: the folder's files [(file_id, name, raw PDF)]: all their pages make the folder's own PDF.
        documents: [(file_id, part name, type, PDF bytes)] from make_folder.
        number_rule: the dossier's import_folder_number (res.company); "file_name" numbers the folder after the first
            file, any other rule leaves the number to Experio's reading.

    ``made`` is the drafts created, [(file_id, part name, invoice_id)].

    Each document that is not a DUM or attachment (NOT_DOCUMENTS) becomes a draft through exp.invoice.import's
    action_add_documents_from_javascript, with defer_bluejay_send so its reading waits for the folder;
    create_folder_from_js links the drafts to the new folder and sends their reading (exp_import_folder.py:786). The DUM
    and attachment pages are attached to the folder as files (action_attach_pages_from_javascript); the DUM is then
    read by Tarik (dum_values) and what it says written on the folder.

    Returns (folder_id, made, rate): ``rate`` is the DUM's rate written, None when the folder has no DUM or its rate
    could not be read."""
    context = {**in_dossier(dossier_id), "default_folder_type": "import"}
    wizard = odoo_call("exp.invoice.import", "create", {"type": "purchase"}, context=context)
    made = []
    for file_id, name, kind, pdf in documents:
        if kind in NOT_DOCUMENTS:
            continue
        page = {"is_scanned": True, "name": name, "mimetype": "application/pdf", "datas": b64(pdf)}
        page["import_invoice_type"] = kind
        held = {**context, "defer_bluejay_send": True}  # sent once the folder holds them (create_folder_from_js)
        ids = odoo_json("exp.invoice.import", "action_add_documents_from_javascript", [wizard], [page], context=held)
        made += [(file_id, name, invoice_id) for invoice_id in ids]
    first_name = group[0][1]
    whole = pages_pdf_all([raw for _, _, raw in group])
    values = {
        "document_pdf": b64(whole),
        "invoice_ids": [[6, 0, [invoice_id for _, _, invoice_id in made]]],
        "ref": (made[0][1] if made else first_name),
        # Experio's screen gives the first file's name for the « file name » rule; its reading sets the others.
        "folder_number": first_name.rsplit(".", 1)[0] if number_rule == "file_name" else False,
    }
    folder_id = odoo_json("exp.import.folder", "create_folder_from_js", values, context=context)
    # Experio reads a DUM only from its screen: its call reads the web request's address (exp_folder_import.py:305),
    # which an RPC call has not ("object is not bound"). The DUM is joined to the folder like an attachment page,
    # and Tarik reads it.
    attached = [{"name": name, "datas": b64(pdf)} for _, name, kind, pdf in documents if kind in ("dum", "attachment")]
    if attached:
        odoo_json(
            "exp.folder.import", "action_attach_pages_from_javascript", [wizard], attached, folder_id, context=context
        )
    dums = [(name, pdf) for _, name, kind, pdf in documents if kind == "dum"]
    values = dum_values(dums[0][1], dossier, dums[0][0], number_rule) if dums else {}
    if values:
        odoo_call("exp.import.folder", "write", [folder_id], values, context=context)
    return folder_id, made, values.get("exchange_rate_dum")


DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def dum_values(pdf, dossier, name, number_rule):
    """Read DUM ``pdf`` with the model and return what Experio's own DUM reading writes on the folder
    (exp_bluejay_job.py:812-834): its exchange rate, date and declared amount, and its number as the folder's when the
    dossier numbers folders by DUM (``number_rule`` « dum_invoice »). {} when no rate could be read: the model failed
    or read none. Writing the rate before the folder's drafts are read is enough: an invoice of a folder takes its DUM
    rate on reading when the dossier's rule is the DUM's (_onchange_exchange_rate_values, exp_invoice.py:3291), and
    Experio passes a new rate to the folder's drafts itself (exp.import.folder.write, exp_import_folder.py:884)."""
    instruction = (
        f"The importer is « {dossier} ». Answer with only a JSON object: "
        '{"exchange_rate": <number or null>, "date": "YYYY-MM-DD" or null, "amount_declared": <number or null>, '
        '"number": "<the DUM\'s number>" or null}'
    )
    try:
        answer = ask_model(
            tool_prompt("read-dum"), pdf_pages(pdf, limit=2), instruction, f"DUM de `{name}`", think=False
        )
    except ToolError as error:
        log(f"read-dum {name}: {error}")
        return {}
    rate = answer.get("exchange_rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0:
        return {}
    values = {"exchange_rate_dum": float(rate)}
    if isinstance(answer.get("date"), str) and DATE.fullmatch(answer["date"]):
        values["date_dum"] = answer["date"]
    amount = answer.get("amount_declared")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
        values["amount_dum_declared"] = float(amount)
    if number_rule == "dum_invoice" and isinstance(answer.get("number"), str) and answer["number"].strip():
        values["folder_number"] = answer["number"].strip()
    return values


def pages_pdf_all(raws):
    """Return all pages of PDFs ``raws``, in order, as one PDF: the folder's own document (document_pdf)."""
    return pdfium(pdfs.merged, raws)


TRANSIT = ("import_invoice_type", "=", "transit")  # an import folder's transit agent invoice


def transit_last(ids, transit):
    """Return invoices ``ids`` with the import folders' transit invoices (``transit``, found with TRANSIT) last.

    The transit invoice must come after the folder's other drafts are posted: Experio fills a transit invoice's
    disbursement invoices only with the folder's posted or exported ones
    (exp.invoice._onchange_autofill_disbursement_invoices, exp_invoice.py:2698)."""
    return sorted(ids, key=lambda i: i in transit)


@step_tool
def create_import_folder(session_id: str, dossier_id: int, client_message: str = "") -> dict:
    """Make the conversation's new files into an import folder in Experio, as Experio's « Imports » screen does: each
    page typed among Experio's own types (transit, douane, DUM, fournisseur, pièce jointe...), each document a draft of
    the folder, the DUM and attachment pages attached to the folder, the DUM read by Tarik (Experio reads a DUM only
    from its own screen) and its rate written on the folder.

    One folder for all the files, or one per file when ``client_message`` (the client's own words) says « plusieurs
    dossiers ».

    Returns:
        An ok result whose ``invoices`` are the drafts made, the transit agent's invoice last, for the invoice's usual
            steps; a stop result (duplicate) when a file's very content already made a folder of the dossier; a question
            result when no file came.

    Raises:
        ToolError: a file is neither a PDF nor an image."""
    dossier = require_dossier(dossier_id)
    _, multiple = folder_mode(client_message)
    with cabinet_db() as db:
        rows = db.execute(Q.folder_files_of_session, (session_id,)).fetchall()
    files = []
    for file_id, name, mime, data in rows:
        if mime != "application/pdf" and not mime.startswith("image/"):
            raise ToolError(f"`{name}` : format non pris en charge ({mime}), envoyez un PDF ou une photo.")
        files.append((file_id, name, to_pdf(bytes(data)) if mime.startswith("image/") else bytes(data)))
    twins = folder_twins(dossier_id, [(file_id, name, bytes(data)) for file_id, name, _, data in rows])
    if twins:
        return stop(
            "Ce dossier d'import a déjà été créé à partir du même fichier ("
            + ", ".join(f"`{name}` : dossier n° {folder}" for name, folder in twins)
            + ") : rien de nouveau créé.",
            duplicate=True,
        )
    if not files:
        return question(
            "Aucun fichier pour le dossier d'import : joignez ses pièces (facture du transitaire, douane, DUM...)."
        )
    with refused(f"create_import_folder {dossier_id}", "Experio n'a pas pu créer le dossier d'import."):
        kinds = page_types()
        rule = odoo_call("res.company", "read", [dossier_id], fields=["import_folder_number", "exchange_rate"])[0]
        folders = make_folder(files, kinds, dossier, multiple)
        said, invoices, unrated = [], [], False
        for group, documents in folders:
            listed = ", ".join(f"{kinds[kind]}" for _, _, kind, _ in documents)
            if dry_run():
                pieces = counted(len(documents), "{} pièce", "{} pièces")
                said.append(f"{TEST_MODE}dossier d'import de {pieces} : {listed}")
                continue
            number_rule = rule["import_folder_number"]
            folder_id, made, rate = create_in_experio(dossier_id, dossier, group, documents, number_rule)
            with cabinet_db() as db:
                for file_id, _, _ in group:
                    db.execute(Q.set_file_folder, (folder_id, file_id))
                for file_id, name, invoice_id in made:
                    (part_id,) = db.execute(
                        Q.insert_cut_invoice_file, (session_id, name, "application/pdf", b"", file_id)
                    ).fetchone()
                    db.execute(Q.set_file_invoice, (invoice_id, part_id))
            ids = [invoice_id for _, _, invoice_id in made]
            invoices += transit_last(ids, odoo_call("exp.invoice", "search", [("id", "in", ids), TRANSIT]))
            said.append(f"dossier d'import n° {folder_id} créé dans le dossier `{dossier}` : {listed}")
            if rate:
                said.append(f"taux de change de la DUM lu et écrit sur le dossier : {rate:g}")
            elif any(kind == "dum" for _, _, kind, _ in documents):
                said.append("la DUM y est jointe, mais je n'ai pas pu lire son taux de change : à saisir dans Experio")
            unrated = unrated or not rate

    if rule["exchange_rate"] == "dum" and unrated:
        # Without a DUM read, Experio takes Bank Al-Maghrib's rate (exp_invoice.py:3286-3304); reading it later
        # changes only the folder's drafts (exp_import_folder.py:884-898).
        said.append(
            "le dossier prend ses taux de change de la DUM : tant que le taux d'une DUM n'est pas sur le dossier "
            "d'import, ses factures en devise ont le taux de « Banque Al Maghrib », et celles comptabilisées avant le "
            "gardent"
        )
    return ok(" ; ".join(said) + ".", invoices=invoices)


def folder_twins(dossier_id, files):
    """Return [(name, folder_id)] for the ``files`` whose very content already made an import folder still in the
    dossier, and link each such file to that folder. ``files``: [(file_id, name, bytes)] as sent."""
    found = []
    with cabinet_db() as db:
        for file_id, name, raw in files:
            for (folder_id,) in db.execute(Q.folder_of_same_content, (raw, file_id)):
                if odoo_call("exp.import.folder", "search", [("id", "=", folder_id), ("company_id", "=", dossier_id)]):
                    found.append((name, folder_id))
                    db.execute(Q.set_file_folder, (folder_id, file_id))
                    break
    return found
