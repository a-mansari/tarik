"""Comparing what people write: names and words, accents, case and punctuation aside."""

import re
import unicodedata
from difflib import SequenceMatcher


def normalized(text):
    """``text`` lowercased, without accents, words only: "Sté  Alliance-Génie" -> "ste alliance genie"."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())  # any run of non-alphanumerics is one space


def said(phrase, words):
    """True when ``phrase`` is in ``words`` as whole words (both normalized): "alpha" is not in "alphanumerique"."""
    # Not preceded nor followed by a letter or digit.
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", words) is not None


def without_if(name):
    """A tiers' name without the " - <IF>" Experio appends to it: "ATLAS - 1234" -> "ATLAS"."""
    return re.sub(r"\s+-\s+\d+\s*$", "", name or "")  # " - " then digits, at the very end


def similarity(query, name):
    """How much ``name`` looks like ``query``, 0..1: misspelled, reordered or partial still score high."""
    query, name = normalized(query), normalized(name)
    if not query or not name:
        return 0.0
    in_order = SequenceMatcher(None, query, name).ratio()
    sorted_words = SequenceMatcher(None, " ".join(sorted(query.split())), " ".join(sorted(name.split()))).ratio()
    score = max(in_order, sorted_words)
    if len(query) >= 3 and set(query.split()) <= set(name.split()):
        score = max(score, 0.9)  # every word given is in the name: "alliance" -> "Ste Alliance Genie Civil"
    return score


def ranked(query, items, minimum=0.0, top=None):
    """``items`` ({name, ...}) as [(score, item)], closest to ``query`` first, at least ``minimum``, ``top`` at most."""
    scored = sorted(((similarity(query, item["name"]), item) for item in items), key=lambda pair: -pair[0])
    return [(score, item) for score, item in scored[:top] if score >= minimum]
