"""What the client sent becomes an invoice draft or a bank statement in Experio."""

import base64
import io
import itertools
import re

import pypdfium2

from tarik.core import gladden
from tarik.core.app import ToolError, ok, question, routine, step_tool, stop
from tarik.core.cabinets import cabinet_db, setting
from tarik.core.config import TEST_MODE
from tarik.core.conversation import client_messages, own_words
from tarik.core.experio import dry_run, in_dossier, odoo_call, refused, require_dossier
from tarik.core.model import pdf_pages, to_pdf
from tarik.core.queries import Q
from tarik.core.words import normalized, said
from tarik.invoices.classify import classify_pages, client_side

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
    """ToolError unless file ``name`` was identified as ``wanted`` ("invoice" or "statement"): a bank statement
    must never become an invoice draft (nor be cut page by page), nor the reverse."""
    if kind is None:
        raise ToolError(f"`{name}` n'est pas encore identifie : appelez d'abord identify_document.")
    if kind != wanted:
        other = "import_bank_statement" if kind == "statement" else "split_invoices puis create_draft_invoice"
        raise ToolError(f"`{name}` est {KIND_LABEL[kind]}, pas {KIND_LABEL[wanted]} : utilisez {other}.")


def client_kind(text):
    """The kind ("invoice" or "statement") the client's own ``text`` gives a file they sent, or None (neither, or
    both said)."""
    words = normalized(text or "")
    found = {kind for kind, pattern in KIND_WORDS.items() if re.search(rf"(?<![a-z0-9])(?:{pattern})", words)}
    return found.pop() if len(found) == 1 else None


def set_kind(file_id, kind):
    """Store ``kind`` for the file, as the client said it is (apply_answer)."""
    with cabinet_db() as db:
        db.execute(Q.set_file_kind, (kind, file_id))


def gladden_type(raw):
    """Gladden's type for the first page of PDF ``raw``, or None: GLADDEN_URL or GLADDEN_CALLBACK_URL not set, or
    no answer."""
    url, callback_url = setting("GLADDEN_URL", ""), setting("GLADDEN_CALLBACK_URL", "")
    return gladden.classify(url, split_pdf(raw, [2])[0][2], callback_url) if url and callback_url else None


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


def identified(name, file_id, kind, reason=""):
    """identify_document's answer: routine for an invoice or a statement, a stop for anything else (nothing is
    made of it)."""
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
    """Call it first for every file the client sends: says whether it is an
    invoice (then split_invoices, create_draft_invoice), a bank statement
    (then import_bank_statement), or neither (nothing is made of it). Nothing else accepts a file it hasn't seen.
    What the client says the file is (``client_message``, their own words) wins, until it is imported."""
    with cabinet_db() as db:
        name, mime, data, kind, invoice_id, statement_id = file_row(
            db, file_id, "file_name", "mime_type", "data", "kind", "invoice_id", "statement_id"
        )
        # Only a correction: of a file already identified, which the client names (a new file's kind is read, a
        # message's "voici une facture et le relevé" never decides it for each of its files).
        words = own_words(client_message)
        named = kind and said(normalized(name.split(" › ")[-1].rsplit(".", 1)[0]), normalized(words))
        said_kind = client_kind(words) if named else None
        if said_kind and said_kind != kind and not invoice_id and not statement_id:
            db.execute(Q.set_file_kind, (said_kind, file_id))
            return routine(f"`{name}` : {KIND_LABEL[said_kind]}, comme vous l'avez dit.", kind=said_kind)
        if kind:
            return identified(name, file_id, kind)
        if mime != "application/pdf" and not mime.startswith("image/"):
            raise ToolError(f"`{name}` : format non pris en charge ({mime}), envoyez un PDF ou une photo.")
        raw = as_pdf(mime, data)
        try:
            pages = pdf_pages(raw, limit=1)
        except pypdfium2.PdfiumError:
            raise ToolError(f"`{name}` : PDF illisible.") from None
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
    """``by_page`` or ``by_document``, as the client's latest message that says one (and only one) of them; else
    None: the client is asked."""
    for message in reversed(messages):
        said = {choice for choice, words in SPLIT_WORDS.items() if re.search(rf"\b(?:{words})\b", normalized(message))}
        if len(said) == 1:
            return said.pop()
    return None


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
    """Call it for every file identify_document found to be an invoice. A PDF of several pages is cut as the
    client says, the two ways Experio's import screen offers: one invoice per page, or one for the whole document;
    until they say, they are asked and nothing is made. Returns the file number of each invoice (the same file when
    it is one); create one draft per number returned.
    ``invoice_type``: achat, vente or mixte, only as the client wrote it in this
    conversation; otherwise the client is asked first and nothing is made."""
    with cabinet_db() as db:
        session_id, name, mime, data, invoice_id, kind, parent_id = file_row(
            db, file_id, "session_id", "file_name", "mime_type", "data", "invoice_id", "kind", "parent_id"
        )
        require_kind(name, kind, "invoice")
        said = client_messages(session_id, client_message)
        if not client_side(invoice_type, " ".join(said)):
            return question(f"`{name}` : s'agit-il de factures d'achat ou de vente ? Rien n'a été créé.")
        parts = db.execute(Q.cut_parts_of_file, (file_id,)).fetchall()
        if parts:
            return ok(
                f"`{name}` : déjà découpé en {len(parts)} factures : {listed(parts)}.", files=[i for i, _ in parts]
            )
        one = routine(f"`{name}` : 1 facture, fichier n° {file_id}.", files=[file_id])
        if invoice_id or parent_id or mime != "application/pdf":  # parent_id: already one invoice cut out of a PDF
            return one
        try:
            count = len(pypdfium2.PdfDocument(bytes(data)))
        except pypdfium2.PdfiumError:
            return ok(f"`{name}` : PDF illisible, 1 document, fichier n° {file_id}.", files=[file_id])
        if count == 1:
            return one
        choice = split_choice(said)
        if choice is None:
            return question(SPLIT_QUESTION.format(name=name, count=count))
        if choice == "by_document":
            return routine(f"`{name}` : 1 facture sur {count} pages, fichier n° {file_id}.", files=[file_id])
        made = save_parts(db, session_id, name, file_id, split_pdf(bytes(data), range(1, count + 1)))
    return ok(f"`{name}` : {len(made)} factures, une par page : {listed(made)}.", files=[i for i, _ in made])


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
                f"`{name}` : facture brouillon n° {existing} déjà créée, déjà en cours de traitement.",
                invoice_id=existing,
            )
        parts = [part for part, _ in db.execute(Q.cut_parts_of_file, (file_id,))]
        if parts:
            raise ToolError(
                f"`{name}` a été découpé : créez les brouillons des fichiers n° {', '.join(map(str, parts))}."
            )
        raw = as_pdf(mime, data)
        if dry_run():  # stop: without a draft, the steps after this one have nothing to work on
            return stop(f"{TEST_MODE}`{name}` : la facture brouillon serait créée dans le dossier {dossier}.")
        # A bare exp.invoice create would skip the import's defaults, pending state and Bluejay OCR job.
        with refused(f"create_draft_invoice: file {file_id}", f"Experio n'a pas pu créer la facture pour {name}."):
            invoice_id = import_pdf("exp.invoice.import", name, raw, dossier_id, type="purchase")
        db.execute(Q.set_file_invoice, (invoice_id, file_id))
    created = f"`{name}` : facture brouillon n° {invoice_id} créée dans le dossier {dossier}."
    return routine(created, invoice_id=invoice_id)


def bank_journals(dossier_id):
    """The dossier's bank journals, and those Experio can import into (a journal without its bank is refused):
    two lists of {id, name, bank_id}."""
    with refused(
        f"bank_journals {dossier_id}", "Experio est injoignable : les journaux de banque n'ont pas pu être lus."
    ):
        journals = odoo_call(
            "exp.journal",
            "search_read",
            [("company_id", "=", dossier_id), ("type", "=", "bank")],
            fields=["id", "name", "bank_id"],
            order="name",
        )
    return journals, [journal for journal in journals if journal["bank_id"]]


# Other names clients give a bank (normalized): its sigle, its old or short name.
BANK_ALIASES = {
    "bank of africa": ("bmce", "boa"),
    "attijariwafa": ("awb", "attijari", "wafa"),
    "banque populaire": ("bcp", "chaabi"),
    "societe generale": ("sgmb",),
    "saham": ("sgmb",),
    "credit du maroc": ("cdm",),
    "credit agricole": ("cam",),
    "cih": ("cih bank",),
    "bmci": ("bnp",),
}


def bank_names(journal):
    """Every name ``journal`` can be called by (normalized): its own, its bank's, the sigle in its bank's name
    (« BANK OF AFRICA (BMCE) »), and the bank's usual other names."""
    bank = journal["bank_id"][1] if journal["bank_id"] else ""
    plain = re.sub(r"\([^)]*\)", " ", bank)  # « BANK OF AFRICA (BMCE) » is also « BANK OF AFRICA »
    names = {normalized(journal["name"]), *(normalized(part) for part in (*bank.split(" - "), *plain.split(" - ")))}
    names |= {normalized(sigle) for sigle in re.findall(r"\(([^)]+)\)", bank)}
    names |= {alias for known, aliases in BANK_ALIASES.items() if said(known, normalized(bank)) for alias in aliases}
    return {name for name in names if name}


def journal_named(usable, words):
    """The one usable journal the client's ``words`` name (by its name, its bank's, its sigle), else None."""
    words = normalized(words or "")
    named = [journal for journal in usable if any(said(name, words) for name in bank_names(journal))]
    return named[0] if len(named) == 1 else None


def statement_journal(name, dossier, dossier_id, journal_id, client_message=""):
    """The usable bank journal (dict) statement ``name`` goes to: ``journal_id``, the dossier's only one, or the
    one the client named in ``client_message`` (« relevé du compte BMCE »). Without one to use, or with several to
    choose from, the stop or question result (a dict with "status")."""
    journals, usable = bank_journals(dossier_id)
    if not usable:
        unset = f" ({', '.join(j['name'] for j in journals)} sans banque renseignee)" if journals else ""
        return stop(
            f"Le dossier {dossier} n'a aucun journal de banque utilisable{unset} : a configurer par le cabinet."
        )
    if not journal_id:
        if len(usable) > 1 and (named := journal_named(usable, own_words(client_message))):
            return named
        if len(usable) > 1:
            listed = ", ".join(f"{j['name']} - {j['bank_id'][1]} (n° {j['id']})" for j in usable)
            return question(
                f"`{name}` : le dossier {dossier} a plusieurs comptes bancaires, lequel ? {listed}",
                choices={j["id"]: f"{j['name']} - {j['bank_id'][1]}" for j in usable},
            )
        return usable[0]
    if journal_id not in {j["id"] for j in usable}:
        raise ToolError(f"journal_id {journal_id} n'est pas un journal de banque utilisable du dossier {dossier}.")
    return next(j for j in usable if j["id"] == journal_id)


@step_tool
def import_bank_statement(file_id: int, dossier_id: int, journal_id: int = 0, client_message: str = "") -> dict:
    """Import a bank statement the client sent into the dossier's bank journal,
    through Experio's own statement import (Experio then reads its lines).
    journal_id: leave 0 when the dossier has one bank journal; with several, the one the client's message names
    (``client_message``, their own words), else the answer lists them and the client chooses. Safe to call again."""
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
            db.execute(Q.set_file_statement, (twin[0], file_id))
            return ok(
                f"`{name}` : relevé déjà importé (n° {twin[0]}), le même fichier envoyé avant.", statement_id=twin[0]
            )
        journal = statement_journal(name, dossier, dossier_id, journal_id, client_message)
        if "status" in journal:
            return journal
        if dry_run():
            return ok(
                f"{TEST_MODE}`{name}` : le relevé serait importé dans le dossier {dossier}, journal {journal['name']}."
            )
        raw = as_pdf(mime, data)
        # Experio's own statement import, as its upload screen uses it: bank code as doctype, Bluejay job.
        with refused(f"import_bank_statement {file_id}", f"Experio n'a pas pu importer le relevé {name}."):
            statement_id = import_pdf(
                "exp.bank.statement.import", name, raw, dossier_id, invoice_journal_id=journal["id"]
            )
        db.execute(Q.set_file_statement, (statement_id, file_id))
    return ok(
        f"`{name}` : relevé importé (n° {statement_id}) dans le dossier {dossier}, journal {journal['name']}.",
        statement_id=statement_id,
    )
