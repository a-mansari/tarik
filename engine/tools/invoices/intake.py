"""Turn what the client sent into invoice drafts or bank statements in Experio: identify each file, cut a multi-page
PDF, create the drafts, import the statements."""

import base64
import io
import itertools
import re

import pypdfium2

from engine.tools.core import gladden
from engine.tools.core.app import ToolError, ok, question, routine, step_tool, stop
from engine.tools.core.cabinets import cabinet_db, setting
from engine.tools.core.config import TEST_MODE
from engine.tools.core.conversation import client_messages, own_words
from engine.tools.core.experio import dossier_lock, dry_run, in_dossier, odoo_call, refused, require_dossier, would
from engine.tools.core.model import pdf_pages, to_pdf
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized, said
from engine.tools.dossiers.banks import BANK_ALIASES, give_bank, journal_bank, make_bank_journal, one_bank_named
from engine.tools.invoices import tpe
from engine.tools.invoices.classify import classify_pages, client_side
from engine.tools.invoices.statement import import_canvas, is_canvas

KIND_LABEL = {"invoice": "une facture", "statement": "un relevé bancaire", "other": "ni une facture ni un relevé"}
# How a client corrects what a file they sent is, in their own words (normalized): it wins over Gladden's and the
# model's reading, as long as the file is not imported yet. Both kinds said, or none: nothing is decided.
KIND_WORDS = {
    "invoice": r"(?:c est|ce sont|il s agit d) (?:une |des )?factures?|(?:pas|ni) (?:un |des )?releves?",
    "statement": r"(?:c est|ce sont|il s agit d) (?:un |des )?releves?|(?:pas|ni) (?:une |des )?factures?",
}
# Gladden's types Tarik takes; any other (or none) leaves the file to the model, and so does "bank" (checked).
GLADDEN_KIND = {"invoice": "invoice", "bank": "statement"}
# What Q.file_locked selects, in its order.
FILE_COLUMNS = ("session_id", "file_name", "mime_type", "data", "kind", "invoice_id", "statement_id", "parent_id")


def require_kind(name, kind, wanted):
    """Raise ToolError unless file ``name``'s stored ``kind`` is ``wanted`` ("invoice" or "statement"), so a bank
    statement never becomes an invoice draft (nor is cut page by page), nor the reverse. Also raises when the file is
    not identified yet (``kind`` None)."""
    if kind is None:
        raise ToolError(f"`{name}` n'est pas encore identifie : appelez d'abord identify_document.")
    if kind != wanted:
        other = "import_bank_statement" if kind == "statement" else "split_invoices puis create_draft_invoice"
        raise ToolError(f"`{name}` est {KIND_LABEL[kind]}, pas {KIND_LABEL[wanted]} : utilisez {other}.")


# Words a file's name is often made of, which the client writes about anything (normalized): a stem of these only
# (« facture », « releve 2 », « scan ») names no file unless written whole, with its extension.
COMMON_NAME_WORDS = {
    "facture",
    "factures",
    "releve",
    "releves",
    "scan",
    "scans",
    "document",
    "documents",
    "doc",
    "docs",
    "image",
    "photo",
    "capture",
    "fichier",
    "piece",
    "pieces",
    "invoice",
    "statement",
    "pdf",
    "copie",
    "img",
}


def file_named(name, words):
    """True when the client's ``words`` name file ``name`` (a zip member by its own name): the whole name with its
    extension, or its stem when the stem has a word of its own (« douane », « mars midas »; not « facture 2 »)."""
    base = name.split(" › ")[-1]
    stem = normalized(base.rsplit(".", 1)[0])
    words = normalized(words)
    distinctive = any(not word.isdigit() and word not in COMMON_NAME_WORDS for word in stem.split())
    return said(normalized(base), words) or (distinctive and said(stem, words))


def client_kind(text):
    """Return the kind ("invoice" or "statement") the client's own ``text`` gives a file, or None when it says neither
    or both."""
    words = normalized(text or "")
    found = {kind for kind, pattern in KIND_WORDS.items() if re.search(rf"(?<![a-z0-9])(?:{pattern})", words)}
    return found.pop() if len(found) == 1 else None


def set_kind(file_id, kind):
    """Store ``kind`` as file ``file_id``'s kind, as the client said it is (apply_answer)."""
    with cabinet_db() as db:
        db.execute(Q.set_file_kind, (kind, file_id))


def gladden_type(raw):
    """Return Gladden's type for the first page of PDF ``raw``; None when GLADDEN_URL or GLADDEN_CALLBACK_URL is not
    set, or Gladden gives no answer."""
    url = setting("GLADDEN_URL", "")
    callback_url = setting("GLADDEN_CALLBACK_URL", "")
    if not url or not callback_url:
        return None
    _, _, first_page = split_pdf(raw, [2])[0]  # page 1 alone, as its own PDF
    return gladden.classify(url, first_page, callback_url)


def file_row(db, file_id, *columns):
    """Return file ``file_id``'s ``columns`` (names from FILE_COLUMNS) as a tuple, its row locked until ``db`` commits.
    Raises ToolError when there is no such file."""
    row = db.execute(Q.file_locked, (file_id,)).fetchone()
    if not row:
        raise ToolError(f"Aucun fichier n° {file_id}.")
    values = dict(zip(FILE_COLUMNS, row, strict=True))
    return tuple(values[column] for column in columns)


def as_pdf(mime, data):
    """Return the file's ``data`` as PDF bytes: an image (by ``mime``) becomes a one-page PDF, anything else is returned
    as is (identify_document lets only PDFs and images through)."""
    return to_pdf(bytes(data)) if mime.startswith("image/") else bytes(data)


def identified(name, file_id, kind, reason=""):
    """Return identify_document's result for file ``name``: a routine result for an invoice or a statement; for ``kind``
    "other", a stop result giving ``reason`` (nothing is made of the file)."""
    if kind == "other":
        why = f" ({reason})" if reason else ""
        return stop(
            f"`{name}` : ni une facture ni un relevé bancaire{why}, je ne le saisis pas. Si c'en est un, dites-le-moi.",
            kind=kind,
            file_id=file_id,
        )
    return routine(f"`{name}` : {KIND_LABEL[kind]} (fichier n° {file_id}).", kind=kind)


@step_tool
def identify_document(file_id: int, client_message: str = "") -> dict:
    """Identify a file the client sent: an invoice (then split_invoices, create_draft_invoice), a bank statement (then
    import_bank_statement), or neither (nothing is made of it). Call it first for every file: no other tool accepts a
    file it has not identified.

    ``client_message``: the client's own words. When they name a file already identified and say what it is, their kind
    wins, as long as the file is not imported yet.

    Otherwise, in order: the kind already stored; Experio's Excel statement template; a TPE statement's own first-page
    text; Gladden's type for page 1; the model on page 1 when Gladden does not say "invoice" or the file is a photo.

    Raises:
        ToolError: the file is not a PDF or an image, or the PDF is unreadable."""
    with cabinet_db() as db:
        name, mime, data, kind, invoice_id, statement_id = file_row(
            db, file_id, "file_name", "mime_type", "data", "kind", "invoice_id", "statement_id"
        )
        # Only a correction: of a file already identified, which the client names (a new file's kind is read, a
        # message's "voici une facture et le relevé" never decides it for each of its files).
        words = own_words(client_message)
        named = kind and file_named(name, words)
        said_kind = client_kind(words) if named else None
        if said_kind and said_kind != kind and not invoice_id and not statement_id:
            db.execute(Q.set_file_kind, (said_kind, file_id))
            return routine(f"`{name}` : {KIND_LABEL[said_kind]}, comme vous l'avez dit.", kind=said_kind)
        if kind:
            return identified(name, file_id, kind)
        if is_canvas(name, data):  # Experio's own Excel statement template
            db.execute(Q.set_file_kind, ("statement", file_id))
            return identified(name, file_id, "statement")
        if mime != "application/pdf" and not mime.startswith("image/"):
            raise ToolError(f"`{name}` : format non pris en charge ({mime}), envoyez un PDF ou une photo.")
        raw = as_pdf(mime, data)
        try:
            pages = pdf_pages(raw, limit=1)
        except pypdfium2.PdfiumError:
            raise ToolError(f"`{name}` : PDF illisible.") from None
        # A TPE statement says so on its first page (« relevé des opérations monétiques », « remises cartes »);
        # Gladden calls it an invoice (it prints « N. FACTURE »).
        if tpe.tpe_network(raw, "")[0]:  # its own text only: a message speaks for all its files
            db.execute(Q.set_file_kind, ("statement", file_id))
            return identified(name, file_id, "statement")
        # The first page decides: an invoice is often followed by its payment's proofs (a cheque, the bank's
        # notice or account extract), which would make the whole file look like a statement.
        # shortcut: a PDF mixing invoices and a statement needs splitting by kind.
        kind, reason = GLADDEN_KIND.get(gladden_type(raw)), ""
        # Gladden's "bank" is checked by the model, which has the last word: Gladden calls a leasing schedule, a
        # contract or a customs slip a bank statement (its first page is a table of amounts and dates). So is a photo
        # Gladden calls an invoice: a screenshot of a software screen otherwise became a draft in Experio.
        if kind != "invoice" or mime.startswith("image/"):
            category, reason = classify_pages(pages, "Identification du document")
            kind = {"rb": "statement", "autre": "other"}.get(category, "invoice")
        db.execute(Q.set_file_kind, (kind, file_id))
    return identified(name, file_id, kind, reason)


# How a PDF of several pages is cut, as Experio's own import screen asks it (split_type): one invoice per page, or
# one invoice for the whole document. Decided by the client's own words only, never by a model.
SPLIT_WORDS = {
    "by_page": r"par page|chaque page|page par page|une facture par page|pages? separees?",
    "by_document": r"par document|tout le document|le document entier|une seule facture|un seul document|en entier",
}
SPLIT_QUESTION = (
    "`{name}` : ce PDF a {count} pages. Comment le découper ? Répondez « par page » (une facture par page) ou "
    "« par document » (une seule facture pour tout le fichier). Rien n'a été créé."
)


def split_choice(messages):
    """Return "by_page" or "by_document" from the latest of the client's ``messages`` that says exactly one of them;
    None when none does (the client is asked)."""
    for message in reversed(messages):
        found = {choice for choice, words in SPLIT_WORDS.items() if re.search(rf"\b(?:{words})\b", normalized(message))}
        if len(found) == 1:
            return found.pop()
    return None


def split_pdf(raw, starts):
    """Cut PDF ``raw`` into one PDF per invoice: [(first page, last page, bytes)], 1-based. ``starts``: the pages that
    begin an invoice; page 1 always does, numbers out of range are dropped."""
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
    """Return [(file id, label)] as "fichier n° 12 (label), fichier n° 13 (label)"."""
    return ", ".join(f"fichier n° {file_id} ({label})" for file_id, label in files)


def save_parts(db, session_id, name, file_id, documents):
    """Save each of ``documents`` (first page, last page, PDF bytes), cut out of file ``file_id`` named ``name``, as its
    own invoice file; return [(new file id, its pages as pages_of reads them)]."""
    stem = name.removesuffix(".pdf")
    made = []
    for first, last, document in documents:
        (new_id,) = db.execute(
            Q.insert_cut_invoice_file,
            (session_id, f"{stem} - {pages_of(first, last)}.pdf", "application/pdf", document, file_id),
        ).fetchone()
        made.append((new_id, pages_of(first, last)))
    return made


def sent_again(db, file_id, name, data, dossier_id):
    """Return the result for a file whose very content already made an invoice of the dossier from another file; None
    when there is none or no ``dossier_id``.

    The result: an ok result saying it is already posted and nothing is made, when that invoice is no longer a draft; a
    stop result when a run works on that draft now; else an ok result with ``resume_invoice`` (the draft's id), so that
    draft is taken up again."""
    if not dossier_id:
        return None
    twin = twin_draft(db, file_id, data, dossier_id)
    if not twin:
        return None
    db.execute(Q.set_file_twin, (twin, file_id))
    invoice = odoo_call("exp.invoice", "read", [twin], fields=["state", "invoice_number"])[0]
    label = f"facture n° {twin}" + (f" ({invoice['invoice_number']})" if invoice["invoice_number"] else "")
    if invoice["state"] != "draft":
        told = f"`{name}` : déjà envoyé, c'est la {label}, déjà comptabilisée : rien de nouveau créé."
        return ok(told, files=[], tell=True)  # tell: split_invoices' ok is otherwise not recorded (QUIET_WHEN_OK)
    if db.execute(Q.invoices_worked_on, ([twin],)).fetchone():
        return stop(f"`{name}` : déjà envoyé, c'est la {label}, en cours de traitement : rien de nouveau créé.")
    return ok(f"`{name}` : déjà envoyé, c'est la {label} : je la reprends.", files=[], resume_invoice=twin, tell=True)


def side_read(name, mime, data, dossier_id):
    """Return (side, reason), side "achat" or "vente", that the scan's first page shows for the dossier (the model,
    given the dossier's name); None when it shows neither plainly (a bank statement, no invoice, the model unsure) or
    the file or dossier can't be read."""
    try:
        dossier = require_dossier(dossier_id) if dossier_id else ""
        category, reason = classify_pages(pdf_pages(as_pdf(mime, data), limit=1), f"Achat ou vente : `{name}`", dossier)
    except ToolError, pypdfium2.PdfiumError:
        return None
    return (category, reason) if category in ("achat", "vente") else None


@step_tool
def split_invoices(file_id: int, invoice_type: str = "", client_message: str = "", dossier_id: int = 0) -> dict:
    """Return the file number of each invoice in a file identify_document found to be an invoice; create one draft per
    number returned (the same file when it holds one invoice).

    A file whose very content already made an invoice of the dossier is that invoice, taken up again while it is a draft
    (sent_again). A PDF of several pages is cut as the client says, the two ways Experio's import screen offers
    (exp.invoice.import split_type): « par page », one invoice per page, or « par document », one for the whole file.
    Until the client says, a question result asks and nothing is made.

    Args:
        file_id: the file.
        invoice_type: achat, vente or mixte, as the client wrote it in this conversation. Without it, the side the scan
            shows for the dossier is used and reported to the client (``invoice_type`` in the result); a question result
            when the scan shows none.
        client_message: the client's message, read with the conversation's others for the side and the cut.
        dossier_id: the dossier, for the duplicate check and the side read.

    Raises:
        ToolError: the file is not identified as an invoice."""
    with cabinet_db() as db:
        session_id, name, mime, data, invoice_id, kind, parent_id = file_row(
            db, file_id, "session_id", "file_name", "mime_type", "data", "invoice_id", "kind", "parent_id"
        )
        require_kind(name, kind, "invoice")
        again = None if invoice_id or parent_id else sent_again(db, file_id, name, bytes(data), dossier_id)
        if again:
            return again
        messages = client_messages(session_id, client_message)
        side, said_side = {}, ""
        if not client_side(invoice_type, " ".join(messages)):
            read = side_read(name, mime, data, dossier_id)
            if not read:
                return question(f"`{name}` : s'agit-il de factures d'achat ou de vente ? Rien n'a été créé.")
            kind, other = ("d'achat", "une vente") if read[0] == "achat" else ("de vente", "un achat")
            side = {"invoice_type": read[0], "tell": True}  # told in the reply (QUIET_WHEN_OK records it then)
            said_side = f"facture {kind} d'après le scan ({read[1]}) ; dites-le-moi si c'est {other}. "
        parts = db.execute(Q.cut_parts_of_file, (file_id,)).fetchall()
        if parts:
            return ok(
                f"`{name}` : {said_side}déjà découpé en {len(parts)} factures : {listed(parts)}.",
                files=[i for i, _ in parts],
                **side,
            )
        told = ok if said_side else routine  # a side read on the scan is told, never left out of the summary
        one = told(f"`{name}` : {said_side}1 facture, fichier n° {file_id}.", files=[file_id], **side)
        if invoice_id or parent_id or mime != "application/pdf":  # parent_id: already one invoice cut out of a PDF
            return one
        try:
            count = len(pypdfium2.PdfDocument(bytes(data)))
        except pypdfium2.PdfiumError:
            return ok(
                f"`{name}` : {said_side}PDF illisible, 1 document, fichier n° {file_id}.", files=[file_id], **side
            )
        if count == 1:
            return one
        choice = split_choice(messages)
        if choice is None:
            return question(SPLIT_QUESTION.format(name=name, count=count))
        if choice == "by_document":
            return told(
                f"`{name}` : {said_side}1 facture sur {count} pages, fichier n° {file_id}.", files=[file_id], **side
            )
        made = save_parts(db, session_id, name, file_id, split_pdf(bytes(data), range(1, count + 1)))
    return ok(
        f"`{name}` : {said_side}{len(made)} factures, une par page : {listed(made)}.",
        files=[i for i, _ in made],
        **side,
    )


def import_pdf(wizard, name, raw, dossier_id, **values):
    """Run Experio's own import ``wizard`` (model name, e.g. exp.invoice.import) on PDF ``raw`` of file ``name`` in the
    dossier, as its upload screen does (its defaults, the pending state, the Bluejay reading job), one record for the
    whole file (split_type by_document); return the id of the record made. ``values``: more of the wizard's own fields.

    Raises:
        RuntimeError: the wizard made no record or several."""
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
def create_draft_invoice(file_id: int, dossier_id: int, invoice_type: str = "", posting: str = "") -> dict:
    """Create a draft invoice in Experio from a file the client sent, in the client's dossier; an ok result gives its
    ``invoice_id``.

    Args:
        file_id: the number shown next to the file name (one split_invoices returned).
        dossier_id: the dossier, from find_dossier.
        invoice_type: the side the client gave (checked by split_invoices). "vente" is imported as a sale, since
            Experio's reading looks up the tiers (and its memorized account) on the side it is given; achat or mixte as
            a purchase, classify_invoice settling each one.
        posting: "oui" or "non" when the client's message asked to post (comptabiliser) or not to: kept for the invoice
            now, since a question on the way (TVA, tiers) ends this run before hold_posting reads it and the answer
            that resumes it does not carry the request.

    Safe to call again for the same file: a stop result names the draft already made. A file whose very content already
    made an invoice of the dossier gives a stop result (duplicate) and nothing new.

    Raises:
        ToolError: the file is not an invoice, or was cut into parts (create their drafts instead)."""
    dossier = require_dossier(dossier_id)
    with cabinet_db() as db:
        fields = ("file_name", "mime_type", "data", "invoice_id", "kind", "session_id")
        name, mime, data, existing, kind, session_id = file_row(db, file_id, *fields)
        require_kind(name, kind, "invoice")
        if existing:  # another run made it and works on it: this one leaves it alone
            return stop(
                f"`{name}` : facture brouillon n° {existing} déjà créée, déjà en cours de traitement.",
                invoice_id=existing,
            )
        parts = [part for part, _ in db.execute(Q.cut_parts_of_file, (file_id,))]
        if parts:
            raise ToolError(
                f"`{name}` a été découpé : créez les brouillons des fichiers n° {', '.join(map(str, parts))}."
            )
        twin = twin_draft(db, file_id, data, dossier_id)
        if twin:
            db.execute(Q.set_file_twin, (twin, file_id))
            return stop(
                f"`{name}` : le même fichier a déjà donné la facture n° {twin} de ce dossier, rien de nouveau créé.",
                duplicate=True,
            )
        raw = as_pdf(mime, data)
        if dry_run():  # stop: without a draft, the steps after this one have nothing to work on
            return stop(f"{TEST_MODE}`{name}` : la facture brouillon serait créée dans le dossier `{dossier}`.")
        # A bare exp.invoice create would skip the import's defaults, pending state and Bluejay OCR job.
        with refused(f"create_draft_invoice: file {file_id}", f"Experio n'a pas pu créer la facture pour {name}."):
            side = "sale" if invoice_type == "vente" else "purchase"
            invoice_id = import_pdf("exp.invoice.import", name, raw, dossier_id, type=side)
        db.execute(Q.set_file_invoice, (invoice_id, file_id))
        if posting in ("oui", "non"):
            db.execute(Q.upsert_held_choice, (invoice_id, session_id, posting == "oui"))
    created = f"`{name}` : facture brouillon n° {invoice_id} créée dans le dossier `{dossier}`."
    return routine(created, invoice_id=invoice_id)


def twin_draft(db, file_id, data, dossier_id):
    """Return the dossier's latest invoice (any state except cancelled) already made from another file of the very same
    content, else None. It first locks on that content, so two copies arriving together wait here until the first one's
    draft is made."""
    db.execute(Q.lock_file_content, (bytes(data),))
    made = [invoice_id for (invoice_id,) in db.execute(Q.invoices_of_same_content, (bytes(data), file_id))]
    if not made:
        return None
    domain = [("id", "in", made), ("company_id", "=", dossier_id), ("state", "!=", "cancel")]
    found = odoo_call("exp.invoice", "search", domain, order="id desc", limit=1)
    return found[0] if found else None


UNSUPPORTED_BANKS = ("bti",)  # Experio's reading does not take their statements (_compute_is_not_supported)
# The client's own words that the files are one statement split in several files (normalized): Tarik joins their
# pages into one PDF before the import (merged_pdf); Experio has no merge of its own for an imported statement.
SAME_STATEMENT = re.compile(
    r"(?<![a-z])(?:meme releve|un seul releve|fusionne[rz]?|a fusionner|le releve en plusieurs)"
)


def bank_code(journal):
    """Return the code of ``journal``'s bank (res.bank.code), "" without a bank. Experio sends that code as the
    reading's doctype, which picks how the statement is read (exp_bank_statement_import.py:223)."""
    if not journal.get("bank_id"):
        return ""
    found = odoo_call("res.bank", "search_read", [("id", "=", journal["bank_id"][0])], fields=["code"])
    return (found[0].get("code") if found else "") or ""


def same_statement_files(db, file_id, client_message):
    """When the client's own words say their files are one statement (SAME_STATEMENT): return the conversation's other
    statement files not imported yet, in the order sent, as [(id, name, mime, data)]; else []."""
    if not SAME_STATEMENT.search(normalized(own_words(client_message))):
        return []
    return db.execute(Q.other_statement_files, (file_id, file_id)).fetchall()


def merged_pdf(raws):
    """Return PDFs ``raws`` merged into one, their pages in order."""
    document = pypdfium2.PdfDocument.new()
    for raw in raws:
        document.import_pages(pypdfium2.PdfDocument(raw))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def bank_journals(dossier_id):
    """Return (journals, usable): the dossier's bank journals, and those of them that have a bank, each {id, name,
    bank_id, partner_id, rib}. Experio refuses to import a statement into a journal without a bank
    (exp.bank.statement.import action_create_bank_statements, exp_bank_statement_import.py:142)."""
    with refused(
        f"bank_journals {dossier_id}", "Experio est injoignable : les journaux de banque n'ont pas pu être lus."
    ):
        journals = odoo_call(
            "exp.journal",
            "search_read",
            [("company_id", "=", dossier_id), ("type", "=", "bank")],
            fields=["id", "name", "bank_id", "partner_id", "rib"],
            order="name",
        )
    return journals, [journal for journal in journals if journal["bank_id"]]


def bank_names(journal):
    """Return every name ``journal`` can be called by (normalized): its own, its bank's, the sigle in its bank's name («
    BANK OF AFRICA (BMCE) »), and the names clients use for the bank (banks.BANK_ALIASES)."""
    bank = journal["bank_id"][1] if journal["bank_id"] else ""
    plain = re.sub(r"\([^)]*\)", " ", bank)  # « BANK OF AFRICA (BMCE) » is also « BANK OF AFRICA »
    parts = [*bank.split(" - "), *plain.split(" - ")]
    names = {normalized(journal["name"]), *(normalized(part) for part in parts)}
    names |= {normalized(sigle) for sigle in re.findall(r"\(([^)]+)\)", bank)}
    names.update(alias for alias, words in BANK_ALIASES.items() if said(words, normalized(bank)))
    return {name for name in names if name}


def journal_named(usable, words):
    """Return the one ``usable`` journal the client's ``words`` name (by its name, its bank's, its sigle); None when
    none or several."""
    words = normalized(words or "")
    named = [journal for journal in usable if any(said(name, words) for name in bank_names(journal))]
    return named[0] if len(named) == 1 else None


BANK_QUESTION = "De quelle banque est ce relevé ?"  # the answer goes back to the import as the client's words


def statement_journal(name, dossier, dossier_id, journal_id, client_message=""):
    """Return the usable bank journal (dict) statement ``name`` goes to, and what Tarik did to have it (French text,
    "" when nothing): ``journal_id`` when given, else chosen_journal's, under the dossier's lock (dossier_lock: the
    statements of one message, run in parallel, never make the same journal twice).

    Otherwise returns chosen_journal's question result.

    Raises:
        ToolError: ``journal_id`` is not a usable bank journal of the dossier."""
    if journal_id:
        _, usable = bank_journals(dossier_id)
        if journal_id not in {j["id"] for j in usable}:
            raise ToolError(
                f"journal_id {journal_id} n'est pas un journal de banque utilisable du dossier `{dossier}`."
            )
        return next(j for j in usable if j["id"] == journal_id), ""
    with (
        dossier_lock(dossier_id),
        refused(f"statement_journal {dossier_id}", f"Dossier `{dossier}` : Experio a refusé le journal de banque."),
    ):
        return chosen_journal(name, dossier, dossier_id, own_words(client_message))


def chosen_journal(name, dossier, dossier_id, words):
    """statement_journal without a journal given: the dossier's journal at the one bank the file's name or the client's
    ``words`` name (« relevé BMCE »), made when the dossier has none there (make_bank_journal); else its only usable
    one, else the one the client names by its own name.

    First, a bank journal without its bank whose RIB or own name says one bank (« BQ AWB ») gets it, as ask_banks does
    (give_bank). A bank named when the dossier has one journal still without its bank goes to that journal: no
    second treasury journal.

    Returns (journal, done), or (a question result, done): asking the bank when the dossier has no usable journal and
    nothing names one (``choices`` {bank_id: name}), or listing the usable journals (``choices``) when several fit."""
    context = in_dossier(dossier_id)
    journals, usable = bank_journals(dossier_id)
    banks = odoo_call("res.bank", "search_read", [], fields=["name", "code"], order="name", context=context)
    done = []
    for journal in journals:
        found, how = (None, "") if journal["bank_id"] else journal_bank(journal, banks)
        if found:
            give_bank(dossier_id, journal, found, context)
            journal["bank_id"] = [found["id"], found["name"]]
            usable.append(journal)
            done.append(f"journal `{journal['name']}` {would('mis')} à la banque `{found['name']}` (d'après {how})")
    bank = one_bank_named(f"{name.rsplit('.', 1)[0]} {words}", banks)  # « relevés BMCE et SGMB »: none taken
    bankless = [j for j in journals if not j["bank_id"]]
    if bank:
        at = [j for j in usable if j["bank_id"][0] == bank["id"]]
        if not at and len(bankless) == 1:  # the dossier's one journal without its bank: this bank's, no new one
            give_bank(dossier_id, bankless[0], bank, context)
            done.append(f"journal `{bankless[0]['name']}` {would('mis')} à la banque `{bank['name']}`")
            return {**bankless[0], "bank_id": [bank["id"], bank["name"]]}, notice(done)
        if not at:
            made = make_bank_journal(dossier_id, bank, context)
            done.append(
                f"journal de banque `{made['name']}` {would('créé')} (code `{made['code']}`, compte "
                f"`{made['account']}`) : le dossier n'en avait pas à cette banque"
            )
            return made, notice(done)
        usable = at
    named = usable[0] if len(usable) == 1 else journal_named(usable, words)
    if named:
        return named, notice(done)
    if not usable:
        return question(
            f"`{name}` : le dossier `{dossier}` n'a pas encore de journal de banque utilisable. {BANK_QUESTION} "
            "Je règle son journal.",
            choices={b["id"]: b["name"] for b in banks},
        ), ""
    accounts = ", ".join(f"`{j['name']}` - `{j['bank_id'][1]}` (n° {j['id']})" for j in usable)
    return question(
        f"`{name}` : le dossier `{dossier}` a plusieurs comptes bancaires, lequel ? {accounts}{notice(done)}",
        choices={j["id"]: f"{j['name']} - {j['bank_id'][1]}" for j in usable},
    ), ""


def notice(done):
    """Return what Tarik did to have the journal, as a sentence for the reply ("" when nothing)."""
    return f" {TEST_MODE if dry_run() else 'Fait : '}{' ; '.join(done)}." if done else ""


def import_tpe_file(db, file_id, name, raw, dossier, dossier_id, journal_id, network, client_message):
    """import_bank_statement for a TPE statement: import PDF ``raw`` through Experio's own TPE import with its
    ``network``, into the bank journal chosen.

    Without ``network``, a question result asks it. Without ``journal_id``, the journal of the dossier's last TPE
    statement is used while still usable (Experio's own default, exp_monetary_statement_import.py:24), else
    statement_journal chooses. Returns an ok result with ``statement_id``, or statement_journal's stop or question
    result."""
    if not network:
        return tpe.network_question(name)
    if not journal_id:
        _, usable = bank_journals(dossier_id)
        last = tpe.last_journal(dossier_id)
        journal_id = last if last in {j["id"] for j in usable} else 0
    journal, done = statement_journal(name, dossier, dossier_id, journal_id, client_message)
    if "status" in journal:
        return journal
    label = tpe.network_label(network)
    if dry_run():
        return ok(f"{TEST_MODE}`{name}` : le relevé TPE {label} serait importé, journal `{journal['name']}`.{done}")
    with refused(f"import_bank_statement {file_id}", f"Experio n'a pas pu importer le relevé TPE {name}."):
        statement_id = tpe.import_tpe(name, raw, dossier_id, journal["id"], network)
    db.execute(Q.set_file_statement, (statement_id, network, file_id))
    return ok(
        f"`{name}` : relevé TPE {label} importé (n° {statement_id}) dans le dossier `{dossier}`, "
        f"journal `{journal['name']}`.{done}",
        statement_id=statement_id,
    )


@step_tool
def import_bank_statement(
    file_id: int, dossier_id: int, journal_id: int = 0, client_message: str = "", network: str = ""
) -> dict:
    """Import a bank statement the client sent into one of the dossier's bank journals, through Experio's own statement
    import (exp.bank.statement.import; Experio then reads its lines). Safe to call again: a file already imported, or
    the same content imported before in the dossier, gives that statement.

    A TPE statement (relevé des opérations monétiques) goes to Experio's own TPE import instead, with its network (read
    on it, else asked). Experio's Excel template goes to its own Excel import. Files the client says are one statement
    (« même relevé ») are imported as one PDF, their pages in the order sent.

    Args:
        file_id: the statement file.
        dossier_id: the dossier.
        journal_id: 0 to choose: the dossier's only bank journal, else the one ``client_message`` names, else a question
            result lists them (``choices``) for the client.
        client_message: the client's own words.
        network: the TPE network the client gave, when asked.

    Returns an ok result with ``statement_id``; a stop result when no journal has a bank, or Experio does not read that
    bank's statements (UNSUPPORTED_BANKS, bank_statement._compute_is_not_supported).

    Raises:
        ToolError: the file is not identified as a statement, or ``journal_id`` is not usable."""
    dossier = require_dossier(dossier_id)
    with cabinet_db() as db:
        name, mime, data, kind, existing = file_row(
            db, file_id, "file_name", "mime_type", "data", "kind", "statement_id"
        )
        require_kind(name, kind, "statement")
        if existing:
            return ok(f"`{name}` : relevé déjà importé (n° {existing}).", statement_id=existing)
        # The same statement sent again (another email, the same zip twice): imported once per dossier. Two copies
        # arriving together wait for each other here, until the first is imported.
        db.execute(Q.lock_file_content, (bytes(data),))
        twin = db.execute(Q.statement_of_same_content, (bytes(data), dossier_id, file_id)).fetchone()
        if twin:
            db.execute(Q.set_file_statement, (twin[0], twin[1], file_id))
            return ok(
                f"`{name}` : relevé déjà importé (n° {twin[0]}), le même fichier envoyé avant.", statement_id=twin[0]
            )
        if not is_canvas(name, data):
            is_tpe, read = tpe.tpe_network(as_pdf(mime, data), own_words(client_message))
            if is_tpe or network:
                return import_tpe_file(
                    db,
                    file_id,
                    name,
                    as_pdf(mime, data),
                    dossier,
                    dossier_id,
                    journal_id,
                    network or read,
                    client_message,
                )
        journal, done = statement_journal(name, dossier, dossier_id, journal_id, client_message)
        if "status" in journal:
            return journal
        if bank_code(journal) in UNSUPPORTED_BANKS:  # Experio's own list (bank_statement._compute_is_not_supported)
            return stop(
                f"`{name}` : Experio ne lit pas encore les relevés de `{journal['bank_id'][1]}` : rien n'a été "
                "importé, à saisir par le cabinet (ou en Excel, sur le canevas d'Experio)." + done
            )
        if is_canvas(name, data):  # Experio's Excel template: its own import, into a statement of this journal
            if dry_run():
                return ok(f"{TEST_MODE}`{name}` : le relevé Excel serait importé, journal `{journal['name']}`.{done}")
            with refused(
                f"import_bank_statement {file_id}", f"Experio n'a pas pu importer le relevé {name} ({{reason}})."
            ):
                statement_id = import_canvas(name, data, dossier_id, journal["id"])
            db.execute(Q.set_file_statement, (statement_id, None, file_id))
            return ok(
                f"`{name}` : relevé Excel importé (n° {statement_id}) dans le dossier `{dossier}`, "
                f"journal `{journal['name']}`.{done}",
                statement_id=statement_id,
            )
        merged = same_statement_files(db, file_id, client_message)
        if dry_run():
            return ok(
                f"{TEST_MODE}`{name}` : le relevé serait importé dans le dossier `{dossier}`, "
                f"journal `{journal['name']}`.{done}"
            )
        raw = as_pdf(mime, data)
        if merged:  # « le même relevé » in several files: one statement, their pages in the order sent (merged_pdf)
            raw = merged_pdf([raw, *(as_pdf(m, d) for _, _, m, d in merged)])
        # Experio's own statement import, as its upload screen uses it: bank code as doctype, Bluejay job.
        with refused(f"import_bank_statement {file_id}", f"Experio n'a pas pu importer le relevé {name}."):
            statement_id = import_pdf(
                "exp.bank.statement.import", name, raw, dossier_id, invoice_journal_id=journal["id"]
            )
        db.execute(Q.set_file_statement, (statement_id, None, file_id))
        for other_id, _, _, _ in merged:
            db.execute(Q.set_file_statement, (statement_id, None, other_id))
        if merged:
            names = ", ".join(f"`{other}`" for _, other, _, _ in merged)
            return ok(
                f"`{name}` et {names} : un seul relevé importé (n° {statement_id}) dans le dossier `{dossier}`, "
                f"journal `{journal['name']}`.{done}",
                statement_id=statement_id,
            )
    return ok(
        f"`{name}` : relevé importé (n° {statement_id}) dans le dossier `{dossier}`, "
        f"journal `{journal['name']}`.{done}",
        statement_id=statement_id,
    )
