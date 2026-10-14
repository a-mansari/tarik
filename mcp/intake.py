"""Phase 2: what the client sent becomes an invoice draft or a bank statement in Experio."""

import base64
import io

import psycopg
import pypdfium2
from mcp.server.mcpserver.exceptions import ToolError

from app import with_ids, ok, question, server, stop
from classify import CLASSIFY_PROMPT
from documents import pdf_pages, to_pdf
from experio import POSTGRES, odoo_call, require_dossier
from model import ask_model


KIND_LABEL = {"invoice": "une facture", "statement": "un releve bancaire"}

def require_kind(name, kind, wanted):
    """Files are identified before anything is made from them: a bank statement must
    never become an invoice draft (nor be cut page by page), nor the reverse."""
    if kind is None:
        raise ToolError(f"{name} n'est pas encore identifie : appelez d'abord identify_document.")
    if kind != wanted:
        other = "import_bank_statement" if kind == "statement" else "split_invoices puis create_draft_invoice"
        raise ToolError(f"{name} est {KIND_LABEL[kind]}, pas {KIND_LABEL[wanted]} : utilisez {other}.")

@server.tool()
@with_ids
def identify_document(file_id: int) -> dict:
    """Call it first for every file the client sends: says whether it is an
    invoice (then split_invoices, create_draft_invoice) or a bank statement
    (then import_bank_statement). Nothing else accepts a file it hasn't seen."""
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select file_name, mime_type, data, kind from tarik_files where id = %s for update", (file_id,)).fetchone()
        if not row:
            raise ToolError(f"Aucun fichier n° {file_id}.")
        name, mime, data, kind = row
        if kind:
            return ok(f"{name} : {KIND_LABEL[kind]} (fichier n° {file_id}).", kind=kind)
        raw = to_pdf(bytes(data)) if mime.startswith("image/") else bytes(data)
        if mime != "application/pdf" and not mime.startswith("image/"):
            raise ToolError(f"{name} : format non pris en charge ({mime}), envoyez un PDF ou une photo.")
        try:
            pages = pdf_pages(raw)
        except pypdfium2.PdfiumError:
            raise ToolError(f"{name} : PDF illisible.") from None
        # shortcut: one decision per file from its first pages; a PDF mixing invoices and a statement needs splitting by kind.
        result = ask_model(CLASSIFY_PROMPT, pages,
                           'Answer with only a JSON object: {"category": "achat"|"vente"|"rb", "reason": "one French sentence"}',
                           "Identification du document")
        if result.get("category") not in ("achat", "vente", "rb"):
            print(f"identify_document {file_id}: unexpected category {result.get('category')!r}", flush=True)
            raise ToolError("Identification du document : echec, reessayez plus tard.")
        kind = "statement" if result["category"] == "rb" else "invoice"
        db.execute("update tarik_files set kind = %s where id = %s", (kind, file_id))
    return ok(f"{name} : {KIND_LABEL[kind]} (fichier n° {file_id}).", kind=kind)

# tarik-master/tarik/agents/pages.py, verbatim.
PAGES_PROMPT = """You are given the pages of one scanned PDF, in order, from a Moroccan
accounting cabinet. It may hold several invoices. Say on which pages a NEW
invoice begins.

A page begins a new invoice when it has its own header: a supplier letterhead,
"Facture N°", a date, a "Facture a" / client box. A page continues the previous
one when it has no such header and goes on with the same lines, or only
carries the totals (HT, TVA, TTC), the signature or the terms.

Page 1 always begins an invoice. When unsure, a page begins a new invoice.
"""

def invoice_starts(pages):
    """1-based pages that begin an invoice, as MiniMax-M3 sees them. ``pages``: PNG bytes, in order."""
    starts = ask_model(PAGES_PROMPT, pages, f'{len(pages)} pages, in order. Answer with only a JSON object: {{"starts": [1, ...]}}',
                       "Decoupage du PDF").get("starts")
    if not isinstance(starts, list) or not all(isinstance(n, int) for n in starts):
        print(f"invoice_starts: starts must be page numbers: {starts}", flush=True)
        raise ToolError("Decoupage du PDF : echec, reessayez plus tard.")
    return starts

def split_pdf(raw, starts):
    """One PDF per invoice, as (first page, last page, bytes). ``starts``: 1-based pages
    that begin an invoice; page 1 always does, numbers out of range are dropped."""
    pdf = pypdfium2.PdfDocument(raw)
    count = len(pdf)
    starts = sorted({1, *(n for n in starts if 1 <= n <= count)})
    documents = []
    for first, after in zip(starts, [*starts[1:], count + 1]):
        document = pypdfium2.PdfDocument.new()
        document.import_pages(pdf, list(range(first - 1, after - 1)))
        buffer = io.BytesIO()
        document.save(buffer)
        documents.append((first, after - 1, buffer.getvalue()))
    return documents

def pages_of(first, last):
    return f"page {first}" if first == last else f"pages {first}-{last}"

@server.tool()
@with_ids
def split_invoices(file_id: int) -> dict:
    """Call it first for every file the client sends: a PDF may hold several
    invoices. Returns the file number of each invoice found (the same file
    when it holds only one); create one draft per number returned."""
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select session_id, file_name, mime_type, data, invoice_id, kind from tarik_files where id = %s for update",
                         (file_id,)).fetchone()
        if not row:
            raise ToolError(f"Aucun fichier n° {file_id}.")
        session_id, name, mime, data, invoice_id, kind = row
        require_kind(name, kind, "invoice")
        parts = db.execute("select id, file_name from tarik_files where parent_id = %s order by id", (file_id,)).fetchall()
        if parts:
            return ok(f"{name} : deja decoupe en {len(parts)} factures : " + ", ".join(f"fichier n° {i} ({n})" for i, n in parts) + ".",
                      files=[i for i, _ in parts])
        if invoice_id or mime != "application/pdf":
            return ok(f"{name} : 1 facture, fichier n° {file_id}.", files=[file_id])
        try:
            pdf = pypdfium2.PdfDocument(bytes(data))
        except pypdfium2.PdfiumError:
            return ok(f"{name} : PDF illisible, 1 document, fichier n° {file_id}.", files=[file_id])
        if len(pdf) == 1:
            return ok(f"{name} : 1 facture, fichier n° {file_id}.", files=[file_id])
        pages = []
        for page in pdf:
            buffer = io.BytesIO()
            page.render(scale=1.0).to_pil().save(buffer, format="PNG")
            pages.append(buffer.getvalue())
        documents = split_pdf(bytes(data), invoice_starts(pages))
        if len(documents) == 1:
            return ok(f"{name} : 1 facture sur {len(pdf)} pages, fichier n° {file_id}.", files=[file_id])
        stem = name.removesuffix(".pdf")
        made, ids = [], []
        for first, last, document in documents:
            label = f"{stem} - {pages_of(first, last)}.pdf"
            (new_id,) = db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, parent_id, kind) values (%s, %s, %s, %s, %s, 'invoice') returning id",
                (session_id, label, "application/pdf", document, file_id)).fetchone()
            made.append(f"fichier n° {new_id} ({pages_of(first, last)})")
            ids.append(new_id)
    return ok(f"{name} : {len(made)} factures : " + ", ".join(made) + ".", files=ids)

@server.tool()
@with_ids
def create_draft_invoice(file_id: int, dossier_id: int) -> dict:
    """Create a draft invoice in Experio from a file the client sent, in the
    client's dossier. file_id is the number shown next to the file name;
    dossier_id comes from find_dossier. Safe to call again for the same file:
    it answers with the draft already made."""
    dossier = require_dossier(dossier_id)
    with psycopg.connect(**POSTGRES) as db:
        # Locked until the draft is recorded, so a retry can't create a second one.
        row = db.execute("select file_name, mime_type, data, invoice_id, kind from tarik_files where id = %s for update",
                         (file_id,)).fetchone()
        if not row:
            raise ToolError(f"Aucun fichier n° {file_id}.")
        name, mime, data, existing, kind = row
        require_kind(name, kind, "invoice")
        if existing:
            return ok(f"{name} : facture brouillon deja creee (n° {existing}).", invoice_id=existing)
        parts = [part for (part,) in db.execute("select id from tarik_files where parent_id = %s order by id", (file_id,))]
        if parts:
            raise ToolError(f"{name} a ete decoupe : creez les brouillons des fichiers n° {', '.join(map(str, parts))}.")
        if mime.startswith("image/"):
            data = to_pdf(bytes(data))
        elif mime != "application/pdf":
            return stop(f"{name} : format non pris en charge ({mime}), envoyez un PDF ou une photo.")
        try:
            # Experio's own import wizard, as the upload screen uses it: its defaults, the
            # pending state and the Bluejay OCR job. A bare exp.invoice create skips all three.
            wizard_id = odoo_call("exp.invoice.import", "create", {
                "data": base64.b64encode(bytes(data)).decode(), "mimetype": "application/pdf",
                "name": name.rsplit(".", 1)[0], "type": "purchase", "company_id": dossier_id,
                "split_type": "by_document", "to_document_manager": False,
            }, context={"allowed_company_ids": [dossier_id]})
            created = odoo_call("exp.invoice.import", "read", [wizard_id], fields=["record_ids"],
                                context={"allowed_company_ids": [dossier_id]})[0]["record_ids"]
            if len(created) != 1:
                raise RuntimeError(f"import wizard {wizard_id} created {created}, expected one invoice")
            invoice_id = created[0]
        except Exception as error:
            print(f"create_draft_invoice: file {file_id}: {error}", flush=True)
            raise ToolError(f"Experio n'a pas pu creer la facture pour {name}.") from None
        db.execute("update tarik_files set invoice_id = %s where id = %s", (invoice_id, file_id))
    return ok(f"{name} : facture brouillon n° {invoice_id} creee dans le dossier {dossier}.", invoice_id=invoice_id)

@server.tool()
@with_ids
def import_bank_statement(file_id: int, dossier_id: int, journal_id: int = 0) -> dict:
    """Import a bank statement the client sent into the dossier's bank journal,
    through Experio's own statement import (Experio then reads its lines).
    journal_id: leave 0 when the dossier has one bank journal; with several,
    the answer lists them and the client chooses. Safe to call again."""
    dossier = require_dossier(dossier_id)
    with psycopg.connect(**POSTGRES) as db:
        # Locked until the statement is recorded, so a retry can't import it twice.
        row = db.execute("select file_name, mime_type, data, kind, statement_id from tarik_files where id = %s for update",
                         (file_id,)).fetchone()
        if not row:
            raise ToolError(f"Aucun fichier n° {file_id}.")
        name, mime, data, kind, existing = row
        require_kind(name, kind, "statement")
        if existing:
            return ok(f"{name} : releve deja importe (n° {existing}).", statement_id=existing)
        try:
            journals = odoo_call("exp.journal", "search_read", [("company_id", "=", dossier_id), ("type", "=", "bank")],
                                 fields=["id", "name", "bank_id"], order="name")
        except Exception as error:
            print(f"import_bank_statement {file_id}: {error}", flush=True)
            raise ToolError("Experio est injoignable : les journaux de banque n'ont pas pu etre lus.") from None
        usable = [j for j in journals if j["bank_id"]]  # Experio refuses a journal without its bank
        if not usable:
            unset = f" ({', '.join(j['name'] for j in journals)} sans banque renseignee)" if journals else ""
            return stop(f"Le dossier {dossier} n'a aucun journal de banque utilisable{unset} : a configurer par le cabinet.")
        if not journal_id:
            if len(usable) > 1:
                return question(f"{name} : le dossier {dossier} a plusieurs comptes bancaires, lequel ? " + ", ".join(
                    f"{j['name']} - {j['bank_id'][1]} (journal_id {j['id']})" for j in usable))
            journal_id = usable[0]["id"]
        elif journal_id not in {j["id"] for j in usable}:
            raise ToolError(f"journal_id {journal_id} n'est pas un journal de banque utilisable du dossier {dossier}.")
        raw = to_pdf(bytes(data)) if mime.startswith("image/") else bytes(data)
        context = {"allowed_company_ids": [dossier_id]}
        try:
            # Experio's own statement import, as its upload screen uses it: bank code as doctype, Bluejay job.
            wizard_id = odoo_call("exp.bank.statement.import", "create", {
                "data": base64.b64encode(raw).decode(), "mimetype": "application/pdf", "name": name.rsplit(".", 1)[0],
                "company_id": dossier_id, "invoice_journal_id": journal_id, "split_type": "by_document",
                "to_document_manager": False}, context=context)
            created = odoo_call("exp.bank.statement.import", "read", [wizard_id], fields=["record_ids"], context=context)[0]["record_ids"]
            if len(created) != 1:
                raise RuntimeError(f"statement import wizard {wizard_id} created {created}, expected one statement")
        except Exception as error:
            print(f"import_bank_statement {file_id}: {error}", flush=True)
            raise ToolError(f"Experio n'a pas pu importer le releve {name}.") from None
        db.execute("update tarik_files set statement_id = %s where id = %s", (created[0], file_id))
    journal = next(j for j in usable if j["id"] == journal_id)
    return ok(f"{name} : releve importe (n° {created[0]}) dans le dossier {dossier}, journal {journal['name']}.", statement_id=created[0])
