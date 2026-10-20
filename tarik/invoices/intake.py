"""What the client sent becomes an invoice draft or a bank statement in Experio."""

import base64
import io
import itertools

import pypdfium2

from tarik.core.app import ToolError, log, ok, question, routine, step_tool, stop
from tarik.core.cabinets import cabinet_db
from tarik.core.config import TEST_MODE
from tarik.core.conversation import client_messages
from tarik.core.experio import dry_run, in_dossier, odoo_call, refused, require_dossier
from tarik.core.model import ask_model, pdf_pages, to_pdf, tool_prompt
from tarik.core.queries import Q
from tarik.invoices.classify import classify_pages, client_side

KIND_LABEL = {"invoice": "une facture", "statement": "un releve bancaire"}
# What Q.file_locked selects, in its order.
FILE_COLUMNS = ("session_id", "file_name", "mime_type", "data", "kind", "invoice_id", "statement_id", "parent_id")


def require_kind(name, kind, wanted):
    """ToolError unless file ``name`` was identified as ``wanted`` ("invoice" or "statement"): a bank statement
    must never become an invoice draft (nor be cut page by page), nor the reverse."""
    if kind is None:
        raise ToolError(f"{name} n'est pas encore identifie : appelez d'abord identify_document.")
    if kind != wanted:
        other = "import_bank_statement" if kind == "statement" else "split_invoices puis create_draft_invoice"
        raise ToolError(f"{name} est {KIND_LABEL[kind]}, pas {KIND_LABEL[wanted]} : utilisez {other}.")


def file_row(db, file_id, *columns):
    """The file's ``columns`` (of tarik_files), as a tuple, its row locked until ``db`` commits; ToolError when there
    is no such file."""
    row = db.execute(Q.file_locked, (file_id,)).fetchone()
    if not row:
        raise ToolError(f"Aucun fichier n° {file_id}.")
    values = dict(zip(FILE_COLUMNS, row, strict=True))
    return tuple(values[column] for column in columns)


def as_pdf(mime, data):
    """The file's ``data`` as PDF bytes: a photo becomes a one-page PDF (identify_document lets only PDFs and
    images through)."""
    return to_pdf(bytes(data)) if mime.startswith("image/") else bytes(data)


@step_tool
def identify_document(file_id: int) -> dict:
    """Call it first for every file the client sends: says whether it is an
    invoice (then split_invoices, create_draft_invoice) or a bank statement
    (then import_bank_statement). Nothing else accepts a file it hasn't seen."""
    with cabinet_db() as db:
        name, mime, data, kind = file_row(db, file_id, "file_name", "mime_type", "data", "kind")
        if kind:
            return routine(f"{name} : {KIND_LABEL[kind]} (fichier n° {file_id}).", kind=kind)
        if mime != "application/pdf" and not mime.startswith("image/"):
            raise ToolError(f"{name} : format non pris en charge ({mime}), envoyez un PDF ou une photo.")
        try:
            pages = pdf_pages(as_pdf(mime, data))
        except pypdfium2.PdfiumError:
            raise ToolError(f"{name} : PDF illisible.") from None
        # shortcut: one decision per file from its first pages;
        # a PDF mixing invoices and a statement needs splitting by kind.
        category, _ = classify_pages(pages, "Identification du document")
        kind = "statement" if category == "rb" else "invoice"
        db.execute(Q.set_file_kind, (kind, file_id))
    return routine(f"{name} : {KIND_LABEL[kind]} (fichier n° {file_id}).", kind=kind)


def invoice_starts(pages):
    """1-based pages that begin an invoice, as MiniMax-M3 sees them. ``pages``: PNG bytes, in order."""
    shape = f'{len(pages)} pages, in order. Answer with only a JSON object: {{"starts": [1, ...]}}'
    starts = ask_model(tool_prompt("split-pdf"), pages, shape, "Decoupage du PDF").get("starts")
    if not isinstance(starts, list) or not all(isinstance(n, int) for n in starts):
        log(f"invoice_starts: starts must be page numbers: {starts}")
        raise ToolError("Decoupage du PDF : echec, reessayez plus tard.")
    return starts


def split_pdf(raw, starts):
    """One PDF per invoice, as (first page, last page, bytes). ``starts``: 1-based pages
    that begin an invoice; page 1 always does, numbers out of range are dropped."""
    pdf = pypdfium2.PdfDocument(raw)
    count = len(pdf)
    starts = sorted({1, *(n for n in starts if 1 <= n <= count)})
    documents = []
    for first, after in itertools.pairwise([*starts, count + 1]):
        document = pypdfium2.PdfDocument.new()
        document.import_pages(pdf, list(range(first - 1, after - 1)))
        buffer = io.BytesIO()
        document.save(buffer)
        documents.append((first, after - 1, buffer.getvalue()))
    return documents


def pages_of(first, last):
    """The pages ``first``..``last`` as the client reads them: "page 2" or "pages 2-4"."""
    return f"page {first}" if first == last else f"pages {first}-{last}"


def listed(files):
    """[(file id, label)] as "fichier n° 12 (label), fichier n° 13 (label)"."""
    return ", ".join(f"fichier n° {file_id} ({label})" for file_id, label in files)


def save_parts(db, session_id, name, file_id, documents):
    """Save each of ``documents`` (first page, last page, PDF bytes) cut out of file ``file_id`` (named ``name``)
    as its own invoice file; [(new file id, its pages)]."""
    stem = name.removesuffix(".pdf")
    made = []
    for first, last, document in documents:
        (new_id,) = db.execute(
            Q.insert_cut_invoice_file,
            (session_id, f"{stem} - {pages_of(first, last)}.pdf", "application/pdf", document, file_id),
        ).fetchone()
        made.append((new_id, pages_of(first, last)))
    return made


@step_tool
def split_invoices(file_id: int, invoice_type: str = "", client_message: str = "") -> dict:
    """Call it for every file identify_document found to be an invoice: a PDF
    may hold several invoices. Returns the file number of each invoice found (the same file
    when it holds only one); create one draft per number returned.
    ``invoice_type``: achat, vente or mixte, only as the client wrote it in this
    conversation; otherwise the client is asked first and nothing is made."""
    with cabinet_db() as db:
        session_id, name, mime, data, invoice_id, kind, parent_id = file_row(
            db, file_id, "session_id", "file_name", "mime_type", "data", "invoice_id", "kind", "parent_id"
        )
        require_kind(name, kind, "invoice")
        if not client_side(invoice_type, " ".join(client_messages(session_id, client_message))):
            return question(f"{name} : s'agit-il de factures d'achat ou de vente ? Rien n'a ete cree.")
        parts = db.execute(Q.cut_parts_of_file, (file_id,)).fetchall()
        if parts:
            return ok(f"{name} : deja decoupe en {len(parts)} factures : {listed(parts)}.", files=[i for i, _ in parts])
        one = routine(f"{name} : 1 facture, fichier n° {file_id}.", files=[file_id])
        if invoice_id or parent_id or mime != "application/pdf":  # parent_id: already one invoice cut out of a PDF
            return one
        try:
            count = len(pypdfium2.PdfDocument(bytes(data)))
        except pypdfium2.PdfiumError:
            return ok(f"{name} : PDF illisible, 1 document, fichier n° {file_id}.", files=[file_id])
        if count == 1:
            return one
        documents = split_pdf(bytes(data), invoice_starts(pdf_pages(bytes(data), limit=None, scale=1.0)))
        if len(documents) == 1:
            return routine(f"{name} : 1 facture sur {count} pages, fichier n° {file_id}.", files=[file_id])
        made = save_parts(db, session_id, name, file_id, documents)
    return ok(f"{name} : {len(made)} factures : {listed(made)}.", files=[i for i, _ in made])


def import_pdf(wizard, name, raw, dossier_id, **values):
    """Run Experio's own import ``wizard`` (model name) on PDF ``raw`` of file ``name`` in the dossier, as its
    upload screen does (its defaults, the pending state, the Bluejay job): the id of the one record it made.
    ``values``: the wizard's own fields. RuntimeError unless it made exactly one."""
    context = in_dossier(dossier_id)
    values |= {
        "data": base64.b64encode(raw).decode(),
        "mimetype": "application/pdf",
        "name": name.rsplit(".", 1)[0],
        "company_id": dossier_id,
        "split_type": "by_document",
        "to_document_manager": False,
    }
    wizard_id = odoo_call(wizard, "create", values, context=context)
    created = odoo_call(wizard, "read", [wizard_id], fields=["record_ids"], context=context)[0]["record_ids"]
    if len(created) != 1:
        raise RuntimeError(f"{wizard} wizard {wizard_id} created {created}, expected one record")
    return created[0]


@step_tool
def create_draft_invoice(file_id: int, dossier_id: int) -> dict:
    """Create a draft invoice in Experio from a file the client sent, in the
    client's dossier. file_id is the number shown next to the file name;
    dossier_id comes from find_dossier. Safe to call again for the same file:
    it answers with the draft already made."""
    dossier = require_dossier(dossier_id)
    with cabinet_db() as db:
        name, mime, data, existing, kind = file_row(db, file_id, "file_name", "mime_type", "data", "invoice_id", "kind")
        require_kind(name, kind, "invoice")
        if existing:  # another run made it and works on it: this one leaves it alone
            return stop(
                f"{name} : facture brouillon n° {existing} deja creee, deja en cours de traitement.",
                invoice_id=existing,
            )
        parts = [part for part, _ in db.execute(Q.cut_parts_of_file, (file_id,))]
        if parts:
            raise ToolError(
                f"{name} a ete decoupe : creez les brouillons des fichiers n° {', '.join(map(str, parts))}."
            )
        raw = as_pdf(mime, data)
        if dry_run():  # stop: without a draft, the steps after this one have nothing to work on
            return stop(f"{TEST_MODE}{name} : la facture brouillon serait creee dans le dossier {dossier}.")
        # A bare exp.invoice create would skip the import's defaults, pending state and Bluejay OCR job.
        with refused(f"create_draft_invoice: file {file_id}", f"Experio n'a pas pu creer la facture pour {name}."):
            invoice_id = import_pdf("exp.invoice.import", name, raw, dossier_id, type="purchase")
        db.execute(Q.set_file_invoice, (invoice_id, file_id))
    created = f"{name} : facture brouillon n° {invoice_id} creee dans le dossier {dossier}."
    return routine(created, invoice_id=invoice_id)


def bank_journals(dossier_id):
    """The dossier's bank journals, and those Experio can import into (a journal without its bank is refused):
    two lists of {id, name, bank_id}."""
    with refused(
        f"bank_journals {dossier_id}", "Experio est injoignable : les journaux de banque n'ont pas pu etre lus."
    ):
        journals = odoo_call(
            "exp.journal",
            "search_read",
            [("company_id", "=", dossier_id), ("type", "=", "bank")],
            fields=["id", "name", "bank_id"],
            order="name",
        )
    return journals, [journal for journal in journals if journal["bank_id"]]


def statement_journal(name, dossier, dossier_id, journal_id):
    """The usable bank journal (dict) statement ``name`` goes to: ``journal_id``, or the dossier's only one.
    Without one to use, or with several to choose from, the stop or question result (a dict with "status")."""
    journals, usable = bank_journals(dossier_id)
    if not usable:
        unset = f" ({', '.join(j['name'] for j in journals)} sans banque renseignee)" if journals else ""
        return stop(
            f"Le dossier {dossier} n'a aucun journal de banque utilisable{unset} : a configurer par le cabinet."
        )
    if not journal_id:
        if len(usable) > 1:
            listed = ", ".join(f"{j['name']} - {j['bank_id'][1]} (n° {j['id']})" for j in usable)
            return question(
                f"{name} : le dossier {dossier} a plusieurs comptes bancaires, lequel ? {listed}",
                choices={j["id"]: f"{j['name']} - {j['bank_id'][1]}" for j in usable},
            )
        return usable[0]
    if journal_id not in {j["id"] for j in usable}:
        raise ToolError(f"journal_id {journal_id} n'est pas un journal de banque utilisable du dossier {dossier}.")
    return next(j for j in usable if j["id"] == journal_id)


@step_tool
def import_bank_statement(file_id: int, dossier_id: int, journal_id: int = 0) -> dict:
    """Import a bank statement the client sent into the dossier's bank journal,
    through Experio's own statement import (Experio then reads its lines).
    journal_id: leave 0 when the dossier has one bank journal; with several,
    the answer lists them and the client chooses. Safe to call again."""
    dossier = require_dossier(dossier_id)
    with cabinet_db() as db:
        name, mime, data, kind, existing = file_row(
            db, file_id, "file_name", "mime_type", "data", "kind", "statement_id"
        )
        require_kind(name, kind, "statement")
        if existing:
            return ok(f"{name} : releve deja importe (n° {existing}).", statement_id=existing)
        journal = statement_journal(name, dossier, dossier_id, journal_id)
        if "status" in journal:
            return journal
        if dry_run():
            return ok(
                f"{TEST_MODE}{name} : le releve serait importe dans le dossier {dossier}, journal {journal['name']}."
            )
        raw = as_pdf(mime, data)
        # Experio's own statement import, as its upload screen uses it: bank code as doctype, Bluejay job.
        with refused(f"import_bank_statement {file_id}", f"Experio n'a pas pu importer le releve {name}."):
            statement_id = import_pdf(
                "exp.bank.statement.import", name, raw, dossier_id, invoice_journal_id=journal["id"]
            )
        db.execute(Q.set_file_statement, (statement_id, file_id))
    return ok(
        f"{name} : releve importe (n° {statement_id}) dans le dossier {dossier}, journal {journal['name']}.",
        statement_id=statement_id,
    )
