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


# The Markdown marks of Tarik's replies, read the same way by the email (engine/tools/emails/sending.py) and the
# WhatsApp (engine/tools/core/sparrow.py) renderers.
BOLD = re.compile(r"\*\*(.+?)\*\*|(?<!\w)__(.+?)__(?!\w)")  # **text** or __text__, never inside a word (a__b__c)
# *text*: single stars, not inside a word nor next to another star, the text not starting or ending with a space.
ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
CODE = re.compile(r"`([^`\n]+)`")  # `text` on one line: a file name, kept as written
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")  # [label](http(s) address)

LIGATURES = str.maketrans({"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae", "ß": "ss", "ø": "o", "Ø": "o"})


def normalized(text):
    """Return ``text`` lowercased, without accents, words only: "Sté  Alliance-Génie" -> "ste alliance genie".

    Any character that is not a letter or digit is a space, whatever its script: the typographic apostrophe of an
    email client (« d’achat ») separates words like the plain one, it never joins them (« dachat »)."""
    decomposed = unicodedata.normalize("NFKD", text.translate(LIGATURES))
    plain = "".join(char for char in decomposed if not unicodedata.combining(char)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())  # any run of non-alphanumerics is one space


def said(phrase, words):
    """True when ``phrase`` is in ``words`` as whole words (both normalized): "alpha" is not in "alphanumerique"."""
    # Not preceded nor followed by a letter or digit.
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", words) is not None


def without_if(name):
    """Return a tiers' name without the " - <IF>" Experio appends to it: "ATLAS - 1234" -> "ATLAS"."""
    return re.sub(r"\s+-\s+\d+\s*$", "", name or "")  # " - " then digits, at the very end


LEGAL_FORMS = {"ste", "societe", "sarl", "sarlau", "sa", "sas", "au", "ets", "etablissements", "cie"}
LEGAL_FORMS |= {"ltd", "limited", "llc", "inc", "corp", "corporation", "co", "company", "gmbh", "ag", "bv", "nv"}
# Foreign suppliers' too: the scan prints them, the dossier's tiers may not.
LEGAL_FORMS |= {"spa", "srl", "plc", "pte", "pvt", "pty", "sl"}


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


def dirhams(amount, currency="MAD"):
    """Return ``amount`` in the invoice's ``currency`` as the client reads it: « 120 000 DH », « 1 234,50 EUR »;
    "" without one. Experio's amount_total is in the invoice's own currency (amount_total_currency is in MAD)."""
    if not amount:
        return ""
    text = f"{amount:,.2f}".replace(",", " ").replace(".", ",").removesuffix(",00")
    return f"{text} {'DH' if currency == 'MAD' else currency}"


def counted(count, one, many):
    """Return the phrase that agrees with ``count`` (French: 0 and 1 singular), its "{}" the count: counted(3,
    "{} opération lue", "{} opérations lues") -> "3 opérations lues"; never "opération(s)"."""
    return (one if count <= 1 else many).format(count)


SHOWN = 10  # the items a report names before saying how many others


def listed(items, total=None):
    """Return ``items`` (phrases) joined by « ; », the first SHOWN then how many others of ``total`` (default: all
    items): listed(["A", "B"], 12) -> "A ; B ; et 10 autres"."""
    more = (len(items) if total is None else total) - min(len(items), SHOWN)
    return " ; ".join(items[:SHOWN]) + (counted(more, " ; et {} autre", " ; et {} autres") if more > 0 else "")


def operation_phrase(line, amount):
    """Return a bank operation as a report names it: « libellé (date, montant) », « sans libellé » when it has none."""
    return f"{line['payment_ref'] or 'sans libellé'} ({line['date']}, {abs(amount or 0):.2f})"


def reason(why):
    """Return Experio's reason ``why`` as read after a colon: its first letter lowercased unless an acronym (« TVA »,
    « IF »; « L'opération » is not one), without its final "."."""
    return (why if why[1:2].isupper() else why[:1].lower() + why[1:]).rstrip(".")


def reasons(counts, one, many):
    """Return a line "<count> : <reason>." per {Experio's reason: count} in ``counts``, in its order, the count said
    with counted(n, ``one``, ``many``): "2 lignes : taux absent."."""
    return [f"{counted(n, one, many)} : {reason(why)}." for why, n in counts.items()]
