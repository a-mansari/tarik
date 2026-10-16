"""Numbers as clients and the model write them, French style (1 005,50 · 1.005,50) or American (1,005.50).

A number is read every way it can be: most have one reading, a few two ("1,005" is 1.005 in French and 1005
in American). One reading is the value. Two: whoever wrote it decides -- the vision model is asked again about
its scan, and a value the agent gives from the client's words counts when it is one of their readings."""

import re


def readings(value):
    """Every value ``value`` may mean, smallest first; [] when it is no number. 454.55 and 20 are
    themselves; "1 005,50", "1.005,50" and "1,005.50" are 1005.5; "1,005" and "1.005" are 1.005 or 1005."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    # Spaces (narrow and non-breaking ones too), "%" and a trailing currency are no part of the number.
    text = re.sub(r"[\s  %]|dhs?$|mad$|dirhams?$", "", str(value).lower())
    if "," in text and "." in text:  # both: the last one is the decimal point, in either style
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(",", ".")
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
    """The one value a number means: None stays None (not read). No number, or several readings: ValueError,
    saying which readings. Never a guess."""
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
    """Every value the numbers of a text may mean: "capital 100 000,50 DH" -> [100000.5], "1,005" -> [1.005, 1005]."""
    return [reading for found in WRITTEN.findall(text or "") for reading in readings(found)]
