"""Phase 1: finding the client's dossier."""

import re
import unicodedata
from difflib import SequenceMatcher

from tarik.core.app import server
from tarik.core.experio import DOSSIER_DOMAIN, odoo_call


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
