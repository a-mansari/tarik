"""Tarik's Odoo tools, served over MCP for the n8n agent. One tool per step.

Ported from tarik-master/tarik/odoo.py as each step needs it.
"""

import base64
import io
import json
from datetime import date
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
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
# IPv4 only: the container has no IPv6 route, and httpx does not fall back when a host's IPv6 address comes first.
HTTP = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"))  # 1: tools that write to Experio only say what they would write
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


def login():
    """Tarik's uid in the cabinet, checked against ODOO_UID."""
    uid = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common").authenticate(ODOO_DB, ODOO_USER, ODOO_KEY, {})
    if not uid:
        raise PermissionError(f"login refused for {ODOO_USER} on {ODOO_DB}")
    if uid != ODOO_UID:
        raise PermissionError(f"{ODOO_USER} on {ODOO_DB} is uid {uid}, expected {ODOO_UID}: wrong cabinet or user")
    return uid


def odoo_call(model, method, *args, **kw):
    """One authenticated call to Experio's Odoo."""
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return models.execute_kw(ODOO_DB, login(), ODOO_KEY, model, method, list(args), kw)


def odoo_json(model, method, *args, **kw):
    """The same call over JSON-RPC, for answers holding None (onchange), which XML-RPC can't carry."""
    answer = HTTP.post(f"{ODOO_URL}/jsonrpc", timeout=600, json={"jsonrpc": "2.0", "method": "call", "params": {
        "service": "object", "method": "execute_kw",
        "args": [ODOO_DB, login(), ODOO_KEY, model, method, list(args), kw]}}).json()
    if "error" in answer:
        raise RuntimeError(answer["error"].get("data", {}).get("message") or answer["error"].get("message"))
    return answer.get("result")


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
        row = db.execute("select file_name, mime_type, data, invoice_id, kind from tarik_files where id = %s for update",
                         (file_id,)).fetchone()
        if not row:
            return f"Aucun fichier n° {file_id}."
        name, mime, data, existing, kind = row
        require_kind(name, kind, "invoice")
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


def ask_model(system, pages, instruction, what):
    """MiniMax-M3's JSON answer about some page images (PNG bytes, in order).

    MiniMax is called in the Anthropic message format it imitates: hence the
    /v1/messages path and the anthropic-version header. The model is MiniMax's.
    ``what`` names the step in the log and in the agent's error."""
    url, key, model = (os.getenv(name) for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_API_NAME"))
    if not (url and key and model):
        raise ToolError(f"{what} : le modele n'est pas encore configure.")
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(page).decode()}} for page in pages]
    content.append({"type": "text", "text": instruction})
    try:
        answer = HTTP.post(f"{url.rstrip('/')}/v1/messages", timeout=300, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01"}, json={
            "model": model, "max_tokens": 8192, "temperature": 0, "system": system, "messages": [{"role": "user", "content": content}]})
        answer.raise_for_status()
        text = "".join(block.get("text", "") for block in answer.json()["content"])
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception as error:
        print(f"{what}: {error}", flush=True)
        raise ToolError(f"{what} : echec, reessayez plus tard.") from None


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
def split_invoices(file_id: int) -> str:
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
                "insert into tarik_files (session_id, file_name, mime_type, data, parent_id, kind) values (%s, %s, %s, %s, %s, 'invoice') returning id",
                (session_id, label, "application/pdf", document, file_id)).fetchone()
            made.append(f"fichier n° {new_id} ({pages_of(first, last)})")
    return f"{name} : {len(made)} factures : " + ", ".join(made) + "."


# tarik-master/tarik/agents/ocr.py, verbatim.
OCR_PROMPT = """You verify invoices captured in a Moroccan (CGNC) accounting app.

You are given the scan of a supplier invoice, and the values a clerk captured
from it into Odoo. Do two things, in this order:

1. READ THE SCAN. Extract the eight fields yourself, from the image only. Never
   look at the captured values while reading -- they are often wrong, and copying
   them defeats the whole check.
2. COMPARE. Report every field where what you read differs from what was captured.

The fields, and where they are on a Moroccan invoice:

- invoice_number: the "Facture N°" reference. Copy it exactly, including the
  year and any separator. A digit added or dropped is the most common capture error.
- invoice_date_raw: the date of the invoice ("LE :", "Date"), copied exactly as
  printed, digits in the same order, whatever separator is used (e.g. "07/08/2025"
  or "08.07.2025"). Do not interpret it, do not reorder it, do not convert it --
  a plain copy of the three numbers as they appear. Read the year carefully: a
  2024 invoice copied as 2025 is a real error.
- invoice_date_language: "fr" if the invoice's own text (labels, wording) is in
  French, "en" if it is in English. This alone decides day/month order later --
  you do not need to resolve the date yourself.
- amount_untaxed: the H.T total. amount_tva: the TVA line. amount_total: the TTC.
  amount_stamp: "timbre" -- absent on most invoices, which means 0.
  amount_disbursement: "debours" (costs paid on the client's behalf, no TVA)
  -- absent on most invoices, which means 0.
  amount_guarantee_holdback: "retenue de garantie" ("RG"), on BTP invoices --
  withheld from what is paid, NOT part of the TTC. Absent means 0.
  amount_prorata: "prorata" (the share of the site's common costs charged to
  this supplier, BTP) -- deducted like the retenue, NOT part of the TTC.
  Absent means 0.
  amount_ts: "taxe de sejour" (hotels) -- part of the TTC. Absent means 0.
  amount_tsp: "taxe de promotion touristique" ("TPT", "TSP") -- part of the
  TTC. Absent means 0.
  advance_amount: "avance" / "acompte" already paid and deducted on this
  invoice -- NOT part of the TTC. Absent means 0.
  amount_insurance: "assurance" / "assurance BTP" (the site insurance charged
  to this supplier, BTP) -- deducted like the retenue, NOT part of the TTC.
  Absent means 0.
  Moroccan invoices write 83 333,33 for 83333.33: the space groups thousands and
  the comma is the decimal point.
- tier: the SUPPLIER, the company that issued the invoice -- its letterhead is at
  the top and its legal details in the footer. It is NOT the party addressed in
  the box ("client", "facturé à"): that one is the cabinet's own client.
- ice: the supplier's ICE, 15 digits, usually in the footer next to RC / IF /
  CNSS. Take the ICE of the supplier, never the one printed in the client box.
- vat: the supplier's IF (identifiant fiscal), digits only, next to the ICE in
  the footer ("IF", "I.F.", "Id. Fiscal"). Same rule: the supplier's, never
  the client's. Never compared to Odoo -- it helps identify the tiers.
- designation: what was actually billed, copied from the lines of the invoice
  ("MAIN D'OEUVRE", "location de materiel", "honoraires", "carburant"...). Copy
  the wording, do not interpret it. Several lines: join them with ", ". This is
  never compared to Odoo -- it is what lets the expense be classified.
- is_credit_note: true when the document is an "Avoir" / "Facture d'avoir" /
  credit note, false for a normal invoice. Amounts stay positive either way.
- currency: the ISO code of the invoice's currency ("MAD", "EUR", "USD"...).
  "DH", "Dhs" and "dirhams" are MAD.
- country: the ISO 3166 alpha-2 code of the tiers' own address ("MA", "FR",
  "IE", "US"...) -- the supplier for an achat, the customer for a vente.
- payment_method: how it is paid, when printed ("Mode de reglement",
  "Paiement") -- one of especes, cheque, virement, effet, prelevement,
  compensation, or null when not printed. Never compared to Odoo.
- period_start, period_end: the period billed, as YYYY-MM-DD, only when the
  invoice states one ("periode du ... au ...", "abonnement annuel", "loyer de
  janvier a decembre"), else null. Never compared to Odoo.
- tva_lines: one entry per TVA rate in the invoice's tax summary, with its
  rate (e.g. 20), its HT base and its TVA amount. A single-rate invoice gives
  one entry. Never compared to Odoo.

Rules for the comparison:

- Report a field only when it is genuinely different. Formatting is not a
  difference: "83 333,33" and 83333.33 are the same amount, and a supplier name
  captured with its IF number appended is still the same supplier.
- Never report invoice_date_raw or invoice_date_language as a mismatch: they
  are not compared to Odoo at all, only used afterwards to resolve the real
  date in code.
- A field you cannot read on the scan is not a mismatch: leave it out, and set
  its extracted value to null.
- Also report a captured field that is empty while the scan plainly shows a value.
- Beyond the comparison, report an internal inconsistency in what the scan itself
  shows: TTC must equal HT + TVA + stamp + debours + taxe de sejour + TPT, within
  one dirham.
- Give ``problem`` as one French sentence, quoting both values.
- Everything matching: return an empty list.
"""

OCR_SHAPE = """Answer with only one JSON object, of this shape:
{"readable": bool, "extracted": {"invoice_number": str|null, "invoice_date_raw": str|null,
 "invoice_date_language": "fr"|"en", "amount_untaxed": num|null, "amount_tva": num|null,
 "amount_stamp": num|null, "amount_disbursement": num|null, "amount_guarantee_holdback": num|null,
 "amount_prorata": num|null, "amount_insurance": num|null, "amount_ts": num|null, "amount_tsp": num|null,
 "advance_amount": num|null, "amount_total": num|null, "tier": str|null, "designation": str|null,
 "ice": str|null, "vat": str|null, "is_credit_note": bool, "currency": str|null, "payment_method": str|null,
 "period_start": str|null, "period_end": str|null, "country": str|null,
 "tva_lines": [{"rate": num, "amount_ht": num, "amount_tva": num}]},
 "bad_fields": [{"field": str, "stored": str, "extracted": str, "problem": str}]}
invoice_date_language is required: always pick one."""

CHECKED_FIELDS = [
    "invoice_number", "invoice_date", "amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement",
    "amount_guarantee_holdback", "amount_prorata", "amount_insurance", "amount_ts", "amount_tsp",
    "advance_amount", "amount_total", "tier", "ice",
]
MAX_PAGES = 3  # as tarik-master: the first pages carry every field checked


def resolve_date(raw, language):
    """A date copied as printed, plus the invoice's language, as YYYY-MM-DD. Day/month
    order is decided here from the language, never by the model (ocr.py's rule)."""
    match = re.search(r"(\d{1,4})\D(\d{1,4})\D(\d{1,4})", raw or "")
    if not match:
        return None
    a, b, c = match.groups()
    if len(a) == 4:
        year, month, day = a, b, c
    elif len(c) in (2, 4):
        year = c if len(c) == 4 else f"20{c}"
        month, day = (a, b) if (language or "").lower().startswith("en") else (b, a)
    else:
        return None
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def resolve_report(report, stored):
    """The model's report, with the date resolved in code and compared here: the
    model only ever sees the raw date, never the resolved one."""
    if not isinstance(report.get("readable"), bool) or not isinstance(report.get("extracted"), dict) \
            or not isinstance(report["extracted"].get("invoice_date_language"), str) \
            or not isinstance(report.get("bad_fields", []), list):
        raise ValueError(f"report does not match the expected shape: {str(report)[:200]}")
    extracted = report["extracted"]
    resolved = resolve_date(extracted.get("invoice_date_raw"), extracted["invoice_date_language"])
    extracted["invoice_date"] = resolved
    bad = [{key: str(b.get(key)).replace("Odoo", "Experio") for key in ("field", "stored", "extracted", "problem")}
           # Only the compared fields: the prompt forbids reporting invoice_date_raw and co., the model sometimes does.
           for b in report.get("bad_fields", []) if b.get("field") in CHECKED_FIELDS and b.get("field") != "invoice_date"]
    if resolved and str(stored.get("invoice_date")) != resolved:
        raw = extracted.get("invoice_date_raw")
        bad.append({"field": "invoice_date", "stored": str(stored.get("invoice_date")),
                    "extracted": f"{resolved} ({raw})" if raw else resolved,
                    "problem": f"La date capturee ({stored.get('invoice_date')}) differe de celle lue sur le scan ({raw} -> {resolved})."})
    return {"readable": report["readable"], "extracted": extracted, "bad_fields": drop_false_alarms(bad, stored, extracted)}


AMOUNT_FIELDS = {"amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement", "amount_ts", "amount_tsp", "amount_total"}


def drop_false_alarms(bad, stored, extracted):
    """The model's differences, minus the ones measured as false on real invoices:
    equal values, the same supplier written "NAME - IF", and amounts the model
    itself read inconsistently (its HT + TVA + taxes not adding up to its TTC)."""
    def plain(value):
        return re.sub(r"[^0-9a-z]", "", normalized(str(value or "")))
    def amount(name):
        return extracted.get(name) or 0

    parts = sum(amount(name) for name in ("amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement", "amount_ts", "amount_tsp"))
    self_consistent = extracted.get("amount_total") is None or abs(parts - amount("amount_total")) <= 1
    kept = []
    for b in bad:
        field, value = b["field"], stored.get(b["field"])
        read = extracted.get(field)
        if value not in (None, False, "") and read is not None and plain(value) == plain(read):
            continue
        if field == "tier" and value and read and similarity(re.sub(r"\s+-\s+\d+\s*$", "", value), read) >= MIN_SIMILARITY:
            continue
        if field in AMOUNT_FIELDS and not self_consistent:
            print(f"check: amounts read do not add up ({parts} vs {amount('amount_total')}), {field} flag dropped", flush=True)
            continue
        kept.append(b)
    return kept


def require_invoice(invoice_id, fields):
    """The invoice's fields. Every tool working on an invoice calls this first: it fails
    unless the id is a real invoice of one of the cabinet's dossiers."""
    if not isinstance(invoice_id, int) or isinstance(invoice_id, bool) or invoice_id <= 0:
        raise ToolError("invoice_id manquant ou invalide : c'est le numero de facture brouillon donne a sa creation.")
    domain = [("id", "=", invoice_id), *[(f"company_id.{field}", op, value) for field, op, value in DOSSIER_DOMAIN]]
    try:
        found = odoo_call("exp.invoice", "search_read", domain, fields=fields)
    except Exception as error:
        print(f"require_invoice {invoice_id}: {error}", flush=True)
        raise ToolError("Experio est injoignable : la facture n'a pas pu etre lue.") from None
    if not found:
        raise ToolError(f"Aucune facture n° {invoice_id} dans les dossiers du cabinet.")
    return found[0]


@server.tool()
def check_invoice(invoice_id: int) -> str:
    """Check a draft invoice once Experio has read it: compares what Experio
    captured with what the scan shows, and lists every difference. Changes
    nothing. invoice_id is the draft invoice number given at its creation."""
    invoice = require_invoice(invoice_id, ["bluejay_state", "document_pdf", "file_type", *CHECKED_FIELDS[:-2], "partner_id"])
    if invoice["bluejay_state"] in ("pending", "sent"):
        return f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute."
    if invoice["bluejay_state"] != "done":
        return f"Facture {invoice_id} : Experio n'a pas lu cette facture automatiquement, rien a comparer."
    partner = invoice["partner_id"]
    stored = {name: invoice.get(name) for name in CHECKED_FIELDS[:-2]}
    stored["tier"] = partner[1] if partner else None
    stored["ice"] = (odoo_call("exp.partner", "read", [partner[0]], fields=["ice"])[0]["ice"] or None) if partner else None
    if not invoice["document_pdf"] or invoice["file_type"] not in (False, None, "pdf"):
        return f"Facture {invoice_id} : pas de scan PDF a verifier."
    pages = scan_pages(invoice["document_pdf"])
    # The model is told "Experio", never "Odoo": its explanations reach the client.
    # Asked twice when it says "unreadable": MiniMax-M3 sometimes does on a page it reads fine the next time.
    for attempt in range(2):
        answer = ask_model(f"{OCR_PROMPT.replace('Odoo', 'Experio')}\nToday's date is {date.today()}.", pages,
                           "Values captured in Experio:\n" + json.dumps(stored, default=str, ensure_ascii=False, indent=2)
                           + "\n\n" + OCR_SHAPE, "Verification de la facture")
        try:
            report = resolve_report(answer, stored)
        except ValueError as error:
            print(f"check_invoice {invoice_id}: {error}", flush=True)
            raise ToolError("Verification de la facture : echec, reessayez plus tard.") from None
        if report["readable"]:
            break
        print(f"check_invoice {invoice_id}: model said unreadable (attempt {attempt + 1})", flush=True)
    with psycopg.connect(**POSTGRES) as db:
        db.execute("insert into tarik_checks (invoice_id, readable, extracted, bad_fields) values (%s, %s, %s, %s) "
                   "on conflict (invoice_id) do update set readable = excluded.readable, extracted = excluded.extracted, "
                   "bad_fields = excluded.bad_fields, checked_at = now()",
                   (invoice_id, report["readable"], json.dumps(report["extracted"]), json.dumps(report["bad_fields"])))
    if not report["readable"]:
        return f"Facture {invoice_id} : scan illisible."
    if not report["bad_fields"]:
        return f"Facture {invoice_id} : conforme, ce qu'Experio a lu correspond au scan."
    return f"Facture {invoice_id} : {len(report['bad_fields'])} ecart(s) entre Experio et le scan :\n" + "\n".join(
        f"- {b['field']} : {b['problem']}" for b in report["bad_fields"])


# Header fields the correction may write (tarik-master odoo.WRITABLE); tiers, account, journal... are later steps.
WRITABLE = {"invoice_number", "invoice_date", "amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement",
            "amount_guarantee_holdback", "amount_prorata", "amount_insurance", "amount_ts", "amount_tsp",
            "advance_amount", "amount_total"}
MULTI_TVA_RATES = {7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20}  # the amount_tva_<rate> fields Experio has
PAYMENT_METHODS = {"especes": "Esp", "cheque": "Ch", "virement": "Virement", "effet": "Effet",
                   "prelevement": "Pr", "compensation": "Compensation"}


def planned_changes(extracted, bad_fields, invoice):
    """What the check says to write: the differing header fields, the credit note and
    the multi-TVA split (tarik-master orchestrator.correct + odoo.apply_variants)."""
    changes = {b["field"]: extracted[b["field"]] for b in bad_fields
               if b["field"] in WRITABLE and extracted.get(b["field"]) is not None}
    if extracted.get("is_credit_note") and not invoice.get("refund"):
        changes["refund"] = True
    rates = {int(line["rate"]): line["amount_tva"] for line in extracted.get("tva_lines") or []
             if line.get("amount_ht") and line.get("rate") is not None}
    if len(rates) > 1 and not invoice.get("amount_tax_non_recoverable") and set(rates) - {0} <= MULTI_TVA_RATES:
        changes["multi_tva"] = True
        changes.update({f"amount_tva_{rate}": amount for rate, amount in rates.items() if rate})
        changes["amount_tva"] = round(sum(rates.values()), 2)
    return changes


FORM_SKIP = {"one2many", "many2many", "binary", "html", "properties", "json"}
_form = {}


def fill_invoice(invoice_id, changes, context):
    """Type ``changes`` into the invoice form one field at a time, as a user does:
    after each one Experio's onchange fills what it fills on screen (exchange
    rate, withholding...). Then save, unless DRY_RUN. Returns every field that
    ends up changed, Experio's included (tarik-master odoo.fill)."""
    if not _form:
        meta = odoo_call("exp.invoice", "fields_get", [], attributes=["type", "store", "readonly"])
        _form["types"] = {name: m["type"] for name, m in meta.items() if m.get("store") and m["type"] not in FORM_SKIP
                          and not name.startswith("__") and name not in ("id", "create_uid", "write_uid", "create_date", "write_date")}
        _form["editable"] = {name for name, m in meta.items() if not m.get("readonly")}
    types = _form["types"]
    current = odoo_call("exp.invoice", "read", [invoice_id], fields=list(types), context=context)[0]
    values = {k: (v[0] if types.get(k) == "many2one" and v else v) for k, v in current.items() if k in types}
    start = dict(values)
    spec = {name: {} for name in types}
    for name, value in changes.items():
        values[name] = value
        answer = odoo_json("exp.invoice", "onchange", [invoice_id], values, [name], spec, context=context)
        for key, new in ((answer or {}).get("value") or {}).items():
            if key in types:
                values[key] = new.get("id") if isinstance(new, dict) else new
    changed = {k: v for k, v in values.items() if v != start.get(k) and (k in _form["editable"] or k in changes)}
    if changed and not DRY_RUN:
        odoo_call("exp.invoice", "write", [invoice_id], changed, context=context)
    return changed


@server.tool()
def correct_invoice(invoice_id: int) -> str:
    """Correct a draft invoice from its check: writes what check_invoice read on
    the scan over the fields Experio captured differently (amounts, number,
    date, credit note, multi-TVA, currency, payment method), then lets Experio
    rebuild its TVA lines. Call check_invoice first."""
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id", "refund", "amount_tax_non_recoverable",
                                           "payment_method_id", "currency_id"])
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut etre corrigee.")
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select readable, extracted, bad_fields from tarik_checks where invoice_id = %s", (invoice_id,)).fetchone()
    if not row:
        raise ToolError(f"Facture {invoice_id} : pas encore verifiee, appelez d'abord check_invoice.")
    readable, extracted, bad_fields = row
    if not readable:
        return f"Facture {invoice_id} : scan illisible, rien a corriger."
    changes = planned_changes(extracted, bad_fields, invoice)
    notes = []
    try:
        code = (extracted.get("currency") or "MAD").upper()
        if code != "MAD":
            currency = odoo_call("res.currency", "search_read", [("name", "=", code)], fields=["id"], limit=1)
            if currency:
                changes["currency_id"] = currency[0]["id"]  # Experio fetches the rate for the invoice date itself
            else:
                notes.append(f"devise {code} inconnue dans Experio")
        method = PAYMENT_METHODS.get((extracted.get("payment_method") or "").lower())
        if method and not invoice["payment_method_id"]:
            found = odoo_call("exp.payment.method", "search_read", [("name", "=ilike", f"{method}%")], fields=["id"], limit=1)
            if found:
                changes["payment_method_id"] = found[0]["id"]
        if not changes:
            return f"Facture {invoice_id} : rien a corriger." + (f" ({'; '.join(notes)})" if notes else "")
        # Validate against this invoice's own exercice, not the cabinet's latest (odoo._exercice_context).
        context = {"allowed_company_ids": [invoice["company_id"][0]]}
        if invoice["exercice_id"]:
            context["cron_fiscal_entry_id"] = invoice["exercice_id"][0]
        changed = fill_invoice(invoice_id, changes, context)
        if not changed:
            return f"Facture {invoice_id} : deja a jour, rien a changer."
        if not DRY_RUN:
            odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)  # Experio's own "Actualiser"; returns None, hence JSON-RPC
    except Exception as error:
        print(f"correct_invoice {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio a refuse la correction.") from None
    shown = ", ".join(f"{field} = {changed.get(field, value)}" for field, value in changes.items())
    other = sorted(set(changed) - set(changes))
    head = (f"MODE TEST : aucune modification enregistree dans Experio. Facture {invoice_id}, correction prevue : {shown}."
            if DRY_RUN else f"Facture {invoice_id} : corrigee : {shown}.")
    return head + (f" Experio a aussi mis a jour : {', '.join(other)}." if other else "") + (f" ({'; '.join(notes)})" if notes else "")


# tarik-master/tarik/agents/classify.py, verbatim.
CLASSIFY_PROMPT = """You look at a scanned document from a Moroccan accounting cabinet
and decide what it is, before anything else is done with it.

- achat: a supplier invoice ("Facture", "Facture N°") addressed TO the cabinet's
  client -- the cabinet's client is the buyer, named in "Facturé à" / "Bill to".
- vente: an invoice the cabinet's client itself issued to one of ITS OWN
  customers -- the cabinet's client is the seller, its own letterhead is at the
  top billing someone else.
- rb: a releve bancaire (bank statement) -- a list of transactions/movements
  over a period, issued by a bank, not a single invoice with a HT/TVA/TTC total.

Look at the whole page: who bills whom, and whether it is a single invoice or a
table of bank movements. Give a one-sentence reason in French.
"""
CATEGORY_TO_TYPE = {"achat": "purchase", "vente": "sale"}


def scan_pages(document_pdf, scale=150 / 72):
    """The first MAX_PAGES pages of a scan stored in Experio (base64), as PNG bytes."""
    return pdf_pages(base64.b64decode(document_pdf), scale)


def pdf_pages(raw, scale=150 / 72):
    """The first MAX_PAGES pages of a PDF, as PNG bytes."""
    pages = []
    for page in list(pypdfium2.PdfDocument(raw))[:MAX_PAGES]:
        buffer = io.BytesIO()
        page.render(scale=scale).to_pil().save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages


@server.tool()
def classify_invoice(invoice_id: int) -> str:
    """Decide what a draft really is once Experio has read it: a purchase
    (achat), a sale (vente) or a bank statement (rb, not an invoice at all).
    Sets the invoice's type when Experio has it wrong; writes nothing for a
    bank statement."""
    invoice = require_invoice(invoice_id, ["state", "type", "company_id", "exercice_id", "bluejay_state", "document_pdf", "file_type"])
    if invoice["bluejay_state"] in ("pending", "sent"):
        return f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute."
    if not invoice["document_pdf"] or invoice["file_type"] not in (False, None, "pdf"):
        return f"Facture {invoice_id} : pas de scan PDF a classer."
    result = ask_model(CLASSIFY_PROMPT, scan_pages(invoice["document_pdf"]),
                       'Answer with only a JSON object: {"category": "achat"|"vente"|"rb", "reason": "one French sentence"}',
                       "Classement de la facture")
    category, reason = result.get("category"), str(result.get("reason") or "")
    if category not in ("achat", "vente", "rb"):
        print(f"classify_invoice {invoice_id}: unexpected category {category!r}", flush=True)
        raise ToolError("Classement de la facture : echec, reessayez plus tard.")
    with psycopg.connect(**POSTGRES) as db:
        db.execute("insert into tarik_classifications (invoice_id, category, reason) values (%s, %s, %s) "
                   "on conflict (invoice_id) do update set category = excluded.category, reason = excluded.reason, classified_at = now()",
                   (invoice_id, category, reason))
    if category == "rb":
        return f"Facture {invoice_id} : ce n'est pas une facture mais un releve bancaire ({reason}). Rien n'a ete modifie."
    wanted = CATEGORY_TO_TYPE[category]
    if invoice["type"] == wanted:
        return f"Facture {invoice_id} : {category}, deja enregistree comme telle ({reason})."
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : {category}, mais seule une facture brouillon peut changer de type.")
    context = {"allowed_company_ids": [invoice["company_id"][0]]}
    if invoice["exercice_id"]:
        context["cron_fiscal_entry_id"] = invoice["exercice_id"][0]
    try:
        fill_invoice(invoice_id, {"type": wanted}, context)  # Experio's onchange on type runs, as on screen
    except Exception as error:
        print(f"classify_invoice {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio a refuse le changement de type.") from None
    if DRY_RUN:
        return f"MODE TEST : aucune modification enregistree dans Experio. Facture {invoice_id} : {category} ({reason}), le type passerait a {wanted}."
    return f"Facture {invoice_id} : {category} ({reason}), type change en {wanted}."


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
def identify_document(file_id: int) -> str:
    """Call it first for every file the client sends: says whether it is an
    invoice (then split_invoices, create_draft_invoice) or a bank statement
    (then import_bank_statement). Nothing else accepts a file it hasn't seen."""
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select file_name, mime_type, data, kind from tarik_files where id = %s for update", (file_id,)).fetchone()
        if not row:
            raise ToolError(f"Aucun fichier n° {file_id}.")
        name, mime, data, kind = row
        if kind:
            return f"{name} : {KIND_LABEL[kind]} (fichier n° {file_id})."
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
    return f"{name} : {KIND_LABEL[kind]} (fichier n° {file_id})."


@server.tool()
def import_bank_statement(file_id: int, dossier_id: int, journal_id: int = 0) -> str:
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
            return f"{name} : releve deja importe (n° {existing})."
        try:
            journals = odoo_call("exp.journal", "search_read", [("company_id", "=", dossier_id), ("type", "=", "bank")],
                                 fields=["id", "name", "bank_id"], order="name")
        except Exception as error:
            print(f"import_bank_statement {file_id}: {error}", flush=True)
            raise ToolError("Experio est injoignable : les journaux de banque n'ont pas pu etre lus.") from None
        usable = [j for j in journals if j["bank_id"]]  # Experio refuses a journal without its bank
        if not usable:
            unset = f" ({', '.join(j['name'] for j in journals)} sans banque renseignee)" if journals else ""
            return f"Le dossier {dossier} n'a aucun journal de banque utilisable{unset} : a configurer par le cabinet."
        if not journal_id:
            if len(usable) > 1:
                return f"{name} : le dossier {dossier} a plusieurs comptes bancaires, lequel ? " + ", ".join(
                    f"{j['name']} - {j['bank_id'][1]} (journal_id {j['id']})" for j in usable)
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
    return f"{name} : releve importe (n° {created[0]}) dans le dossier {dossier}, journal {journal['name']}."


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
