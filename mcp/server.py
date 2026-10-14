"""Tarik's Odoo tools, served over MCP for the n8n agent. One tool per step.

Ported from tarik-master/tarik/odoo.py as each step needs it.
"""

import base64
import io
import os
import re
import unicodedata
import xmlrpc.client
from difflib import SequenceMatcher

import psycopg
from mcp.server.mcpserver import MCPServer
from PIL import Image

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB = os.environ["ODOO_DB"]
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
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return models.execute_kw(ODOO_DB, uid, ODOO_KEY, model, method, list(args), kw)


def to_pdf(raw):
    """A photo of a facture as a one-page PDF: Experio only takes PDFs."""
    buffer = io.BytesIO()
    Image.open(io.BytesIO(raw)).convert("RGB").save(buffer, format="PDF")
    return buffer.getvalue()


def dossiers():
    """Every dossier (company) Tarik can see in Experio, as [{id, name}]."""
    return odoo_call("res.company", "search_read", [], fields=["id", "name"], order="name")


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


@server.tool()
def create_draft_invoice(file_id: int, dossier_id: int) -> str:
    """Create a draft invoice in Experio from a file the client sent, in the
    client's dossier. file_id is the number shown next to the file name;
    dossier_id comes from find_dossier. Safe to call again for the same file:
    it answers with the draft already made."""
    with psycopg.connect(**POSTGRES) as db:
        # Locked until the draft is recorded, so a retry can't create a second one.
        row = db.execute("select file_name, mime_type, data, invoice_id from tarik_files where id = %s for update",
                         (file_id,)).fetchone()
        if not row:
            return f"Aucun fichier n° {file_id}."
        name, mime, data, existing = row
        if existing:
            return f"{name} : facture brouillon deja creee (n° {existing})."
        if mime.startswith("image/"):
            data = to_pdf(bytes(data))
        elif mime != "application/pdf":
            return f"{name} : format non pris en charge ({mime}), envoyez un PDF ou une photo."
        try:
            found = odoo_call("res.company", "search_read", [("id", "=", dossier_id)], fields=["name"])
            if not found:
                return f"Aucun dossier n° {dossier_id} : cherchez-le avec find_dossier."
            invoice_id = odoo_call("exp.invoice", "create", {
                "company_id": dossier_id, "document_pdf": base64.b64encode(bytes(data)).decode(), "file_type": "pdf"})
        except Exception as error:
            print(f"create_draft_invoice: file {file_id}: {error}", flush=True)
            return f"Experio n'a pas pu creer la facture pour {name}."
        db.execute("update tarik_files set invoice_id = %s where id = %s", (invoice_id, file_id))
    return f"{name} : facture brouillon n° {invoice_id} creee dans le dossier {found[0]['name']}."


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
