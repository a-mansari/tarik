"""Tarik's Odoo tools, served over MCP for the n8n agent. One tool per step.

Ported from tarik-master/tarik/odoo.py as each step needs it.
"""

import base64
import io
import json
import os
import re
import unicodedata
import xmlrpc.client
from difflib import SequenceMatcher

import httpx
import psycopg
import pypdfium2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")


def exact_cabinet(name):
    """The cabinet's database name, taken exactly as configured. No default, no
    trimming, no case change: a name that isn't exact must stop Tarik, never
    land it in another cabinet."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise SystemExit(f"ODOO_DB must be the cabinet's exact database name, got {name!r}")
    return name


def expected_uid(value):
    """The user id Tarik has in this cabinet. A login returning any other id
    means another cabinet (or user): every call then fails."""
    if not value or not value.isdigit() or int(value) <= 0:
        raise SystemExit(f"ODOO_UID must be Tarik's user id in this cabinet, got {value!r}")
    return int(value)


ODOO_DB = exact_cabinet(os.getenv("ODOO_DB"))
ODOO_UID = expected_uid(os.getenv("ODOO_UID"))
ODOO_USER = os.environ["ODOO_USER"]
ODOO_KEY = os.getenv("ODOO_API_KEY") or os.environ["ODOO_PASSWORD"]
POSTGRES = {"host": "postgres", "dbname": os.environ["POSTGRES_DB"], "user": os.environ["POSTGRES_USER"],
            "password": os.environ["POSTGRES_PASSWORD"]}

server = MCPServer("tarik")


@server.tool()
def experio_connection() -> str:
    """Check that Tarik can reach the Experio system and log in. Call it when
    asked whether Experio works, or before anything else when in doubt."""
    # Connection details, and the fact that Experio runs on Odoo, stay in this log: the agent never sees them.
    try:
        version = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common").version()["server_version"]
    except Exception as error:
        print(f"experio_connection: {ODOO_URL} unreachable: {error}", flush=True)
        return "Experio est injoignable pour le moment."
    try:
        # A real read, not only the login: a password can log in yet be refused every call.
        odoo_call("res.company", "search_count", [])
    except Exception as error:
        print(f"experio_connection: {ODOO_USER} on {ODOO_DB} refused: {error}", flush=True)
        return "Experio repond, mais la connexion de Tarik est refusee."
    print(f"experio_connection: ok, Odoo {version}", flush=True)
    return "Connecte au systeme Experio."


def odoo_call(model, method, *args, **kw):
    """One authenticated call to Experio's Odoo."""
    uid = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common").authenticate(ODOO_DB, ODOO_USER, ODOO_KEY, {})
    if not uid:
        raise PermissionError(f"login refused for {ODOO_USER} on {ODOO_DB}")
    if uid != ODOO_UID:
        raise PermissionError(f"{ODOO_USER} on {ODOO_DB} is uid {uid}, expected {ODOO_UID}: wrong cabinet or user")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return models.execute_kw(ODOO_DB, uid, ODOO_KEY, model, method, list(args), kw)


def to_pdf(raw):
    """A photo of a facture as a one-page PDF: Experio only takes PDFs."""
    buffer = io.BytesIO()
    Image.open(io.BytesIO(raw)).convert("RGB").save(buffer, format="PDF")
    return buffer.getvalue()


# Experio's own definition of a client dossier (tax calendar, notifications, bank...):
# enabled, not company 1 (the cabinet itself), not deleted. Archived ones are inactive, hence hidden.
DOSSIER_DOMAIN = [("enabled", "=", True), ("id", "!=", 1), ("is_deleted", "=", False)]


def dossiers():
    """Every dossier (client company) Tarik may work in, as [{id, name}]."""
    return odoo_call("res.company", "search_read", DOSSIER_DOMAIN, fields=["id", "name"], order="name")


# On the real dossiers, right matches score 0.9+ and unrelated ones stay under 0.6.
MIN_SIMILARITY = 0.7


def normalized(name):
    """Lowercase, no accents, words only: "Sté  Alliance-Génie" -> "ste alliance genie"."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_name).split())


def similarity(query, name):
    """0..1. Spelling, word order and a name given in part all still score high."""
    query, name = normalized(query), normalized(name)
    if not query or not name:
        return 0.0
    score = max(SequenceMatcher(None, query, name).ratio(),
                SequenceMatcher(None, " ".join(sorted(query.split())), " ".join(sorted(name.split()))).ratio())
    words = set(name.split())
    if len(query) >= 3 and all(word in words for word in query.split()):
        score = max(score, 0.9)  # every word given is in the name: "alliance" -> "Ste Alliance Genie Civil"
    return score


@server.tool()
def list_dossiers() -> str:
    """List every dossier (client company) in Experio, with its id. Use it when
    the client asks which dossiers exist; to find one dossier, use find_dossier."""
    try:
        found = dossiers()
    except Exception as error:
        print(f"list_dossiers: {error}", flush=True)
        return "Experio n'a pas pu donner la liste des dossiers."
    return f"{len(found)} dossiers :\n" + "\n".join(f"- {c['name']} (id {c['id']})" for c in found)


@server.tool()
def find_dossier(name: str) -> str:
    """Find the dossier the client means from the name they gave, even
    misspelled or partial. Returns the closest dossiers with their id, best
    first. One clear match: use its id. Several close ones: ask the client."""
    try:
        found = dossiers()
    except Exception as error:
        print(f"find_dossier: {error}", flush=True)
        return "Experio n'a pas pu donner la liste des dossiers."
    ranked = sorted(((similarity(name, c["name"]), c) for c in found), key=lambda pair: -pair[0])
    close = [(score, c) for score, c in ranked[:5] if score >= MIN_SIMILARITY]
    if not close:
        return f"Aucun dossier ne ressemble a « {name} »."
    return "\n".join(f"- {c['name']} (id {c['id']}) : ressemblance {round(score * 100)} %" for score, c in close)


def require_dossier(dossier_id):
    """The dossier's name. Every tool working in a dossier calls this first:
    without a real dossier id from find_dossier, it fails instead of guessing.
    ToolError, not ValueError: only a ToolError's message reaches the agent."""
    if not isinstance(dossier_id, int) or isinstance(dossier_id, bool) or dossier_id <= 0:
        raise ToolError("dossier_id manquant ou invalide : trouvez d'abord le dossier avec find_dossier.")
    try:
        found = odoo_call("res.company", "search_read", [("id", "=", dossier_id), *DOSSIER_DOMAIN], fields=["name"])
    except Exception as error:
        print(f"require_dossier {dossier_id}: {error}", flush=True)
        raise ToolError("Experio est injoignable : le dossier n'a pas pu etre verifie.") from None
    if not found:
        raise ToolError(f"Aucun dossier n° {dossier_id} : trouvez-le avec find_dossier.")
    return found[0]["name"]


@server.tool()
def create_draft_invoice(file_id: int, dossier_id: int) -> str:
    """Create a draft invoice in Experio from a file the client sent, in the
    client's dossier. file_id is the number shown next to the file name;
    dossier_id comes from find_dossier. Safe to call again for the same file:
    it answers with the draft already made."""
    dossier = require_dossier(dossier_id)
    with psycopg.connect(**POSTGRES) as db:
        # Locked until the draft is recorded, so a retry can't create a second one.
        row = db.execute("select file_name, mime_type, data, invoice_id from tarik_files where id = %s for update",
                         (file_id,)).fetchone()
        if not row:
            return f"Aucun fichier n° {file_id}."
        name, mime, data, existing = row
        if existing:
            return f"{name} : facture brouillon deja creee (n° {existing})."
        parts = [part for (part,) in db.execute("select id from tarik_files where parent_id = %s order by id", (file_id,))]
        if parts:
            raise ToolError(f"{name} a ete decoupe : creez les brouillons des fichiers n° {', '.join(map(str, parts))}.")
        if mime.startswith("image/"):
            data = to_pdf(bytes(data))
        elif mime != "application/pdf":
            return f"{name} : format non pris en charge ({mime}), envoyez un PDF ou une photo."
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
            return f"Experio n'a pas pu creer la facture pour {name}."
        db.execute("update tarik_files set invoice_id = %s where id = %s", (invoice_id, file_id))
    return f"{name} : facture brouillon n° {invoice_id} creee dans le dossier {dossier}."


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
    """1-based pages that begin an invoice, as MiniMax-M3 sees them. ``pages``: PNG bytes, in order.

    MiniMax is called in the Anthropic message format it imitates: hence the
    /v1/messages path and the anthropic-version header. The model is MiniMax's."""
    url, key, model = (os.getenv(name) for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_API_NAME"))
    if not (url and key and model):
        raise ToolError("Le decoupage des PDF n'est pas encore configure.")
    content = [{"type": "text", "text": f"{len(pages)} pages, in order."}] + [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(page).decode()}}
        for page in pages]
    content.append({"type": "text", "text": 'Answer with only a JSON object: {"starts": [1, ...]}'})
    try:
        answer = httpx.post(f"{url.rstrip('/')}/v1/messages", timeout=300, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01"}, json={
            "model": model, "max_tokens": 1024, "system": PAGES_PROMPT, "messages": [{"role": "user", "content": content}]})
        answer.raise_for_status()
        text = "".join(block.get("text", "") for block in answer.json()["content"])
        starts = json.loads(text[text.index("{"):text.rindex("}") + 1])["starts"]
        if not all(isinstance(n, int) for n in starts):
            raise ValueError(f"starts must be page numbers: {starts}")
    except Exception as error:
        print(f"invoice_starts: {error}", flush=True)
        raise ToolError("Le decoupage du PDF a echoue, reessayez plus tard.") from None
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
def split_invoices(file_id: int) -> str:
    """Call it first for every file the client sends: a PDF may hold several
    invoices. Returns the file number of each invoice found (the same file
    when it holds only one); create one draft per number returned."""
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select session_id, file_name, mime_type, data, invoice_id from tarik_files where id = %s for update",
                         (file_id,)).fetchone()
        if not row:
            raise ToolError(f"Aucun fichier n° {file_id}.")
        session_id, name, mime, data, invoice_id = row
        parts = db.execute("select id, file_name from tarik_files where parent_id = %s order by id", (file_id,)).fetchall()
        if parts:
            return f"{name} : deja decoupe en {len(parts)} factures : " + ", ".join(f"fichier n° {i} ({n})" for i, n in parts) + "."
        if invoice_id or mime != "application/pdf":
            return f"{name} : 1 facture, fichier n° {file_id}."
        try:
            pdf = pypdfium2.PdfDocument(bytes(data))
        except pypdfium2.PdfiumError:
            return f"{name} : PDF illisible, 1 document, fichier n° {file_id}."
        if len(pdf) == 1:
            return f"{name} : 1 facture, fichier n° {file_id}."
        pages = []
        for page in pdf:
            buffer = io.BytesIO()
            page.render(scale=1.0).to_pil().save(buffer, format="PNG")
            pages.append(buffer.getvalue())
        documents = split_pdf(bytes(data), invoice_starts(pages))
        if len(documents) == 1:
            return f"{name} : 1 facture sur {len(pdf)} pages, fichier n° {file_id}."
        stem = name.removesuffix(".pdf")
        made = []
        for first, last, document in documents:
            label = f"{stem} - {pages_of(first, last)}.pdf"
            (new_id,) = db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, parent_id) values (%s, %s, %s, %s, %s) returning id",
                (session_id, label, "application/pdf", document, file_id)).fetchone()
            made.append(f"fichier n° {new_id} ({pages_of(first, last)})")
    return f"{name} : {len(made)} factures : " + ", ".join(made) + "."


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
