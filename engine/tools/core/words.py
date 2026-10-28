"""What people write: names and words compared accents, case and punctuation aside; and numbers as clients and the
model write them, French style (1 005,50 · 1.005,50) or American (1,005.50).

A number is read every way it can be: most have one reading, a few two ("1,005" is 1.005 in French and 1005
in American). One reading is the value. Two: whoever wrote it decides -- the vision model is asked again about
its scan, and a value the agent gives from the client's words counts when it is one of their readings."""

import re
import unicodedata
from difflib import SequenceMatcher

TAG = re.compile(r"</?[a-zA-Z][^<>]*>")  # an HTML tag, not "a < b"
ICE_WRITTEN = re.compile(r"(?<!\d)\d{15}(?!\d)")  # 15 digits, not inside a longer number: an ICE


def strip_tags(html, by=""):
    """Return ``html`` with each tag replaced by ``by``."""
    return TAG.sub(by, html or "")


def normalized(text):
    """Return ``text`` lowercased, without accents, words only: "Sté  Alliance-Génie" -> "ste alliance genie"."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())  # any run of non-alphanumerics is one space


def said(phrase, words):
    """True when ``phrase`` is in ``words`` as whole words (both normalized): "alpha" is not in "alphanumerique"."""
    # Not preceded nor followed by a letter or digit.
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", words) is not None


def without_if(name):
    """Return a tiers' name without the " - <IF>" Experio appends to it: "ATLAS - 1234" -> "ATLAS"."""
    return re.sub(r"\s+-\s+\d+\s*$", "", name or "")  # " - " then digits, at the very end


LEGAL_FORMS = {"ste", "societe", "sarl", "sarlau", "sa", "sas", "au", "ets", "etablissements", "cie"}


def tiers_key(name):
    """Return a tiers' name as looked for in a bank label: normalized, without the « - <IF> » Experio appends nor its
    legal form (« STE ATLAS SARL » is « atlas »); "" when under 4 letters are left, too short to tell."""
    words = [word for word in normalized(without_if(name)).split() if word not in LEGAL_FORMS]
    key = " ".join(words)
    return key if len(key.replace(" ", "")) >= 4 else ""


def similarity(query, name):
    """Return how much ``name`` looks like ``query``, 0..1: misspelled, reordered or partial still score high."""
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
    """Return ``items`` (dicts with "name") as [(score, item)], closest to ``query`` first: those scoring at least
    ``minimum`` among the ``top`` best (None: all)."""
    scored = sorted(((similarity(query, item["name"]), item) for item in items), key=lambda pair: -pair[0])
    return [(score, item) for score, item in scored[:top] if score >= minimum]


# --- Numbers ---


def readings(value):
    """Return every value (float) ``value`` may mean, smallest first; [] when it is no number. 454.55 and 20 are
    themselves; "1 005,50", "1.005,50" and "1,005.50" are 1005.5; "1,005" and "1.005" are 1.005 or 1005."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    # Spaces (narrow and non-breaking ones too), "%" and a trailing currency are no part of the number.
    text = re.sub(r"[\s  %]|dhs?$|mad$|dirhams?$", "", str(value).lower())
    if "," in text and "." in text:  # both: the last one is the decimal point, in either style
        thousands = "." if text.rfind(",") > text.rfind(".") else ","
        text = text.replace(thousands, "").replace(",", ".")
        return [float(text)] if re.fullmatch(r"-?\d+(\.\d+)?", text) else []  # digits, maybe a decimal part
    found = set()
    for mark in ",.":
        if mark in text:
            if re.fullmatch(rf"-?\d+\{mark}\d+", text):  # digits, the mark once, digits: a decimal point
                found.add(float(text.replace(mark, ".")))
            if re.fullmatch(rf"-?\d{{1,3}}(\{mark}\d{{3}})+", text):  # 1 to 3 digits, then groups of 3: thousands
                found.add(float(text.replace(mark, "")))
            return sorted(found)
    return [float(text)] if re.fullmatch(r"-?\d+", text) else []  # a whole number


def as_number(value):
    """Return the one value (float) ``value`` means; None stays None (not read). Raises ValueError when it is no
    number or has several readings (naming them): never a guess."""
    if value is None:
        return None
    found = readings(value)
    if not found:
        raise ValueError(f"not a number: {value!r}")
    if len(found) > 1:
        raise ValueError(f"{value!r} reads as {' or '.join(f'{reading:g}' for reading in found)}")
    return found[0]


# A number in a text: 1 to 3 digits then groups of 3 (thousands spaced or dotted or commaed), maybe a decimal part;
# or plain digits, maybe a decimal part.
WRITTEN = re.compile(r"\d{1,3}(?:[   .,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def written_numbers(text):
    """Return every value the numbers in ``text`` may mean: "capital 100 000,50 DH" -> [100000.5], "1,005" -> [1.005,
    1005]."""
    values = []
    for number in WRITTEN.findall(text or ""):
        values += readings(number)
    return values
