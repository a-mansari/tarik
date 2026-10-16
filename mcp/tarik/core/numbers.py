"""Numbers as clients and the model write them: spaces or dots between thousands, a decimal comma."""

import re


def as_number(value):
    """A number as a number: 454.55, "454,55", "1 005,50", "1.005,50", "20 %", "120 DH". None stays None
    (not read). Anything else is no number: ValueError, never a guess."""
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = re.sub(r"[\s\u00a0\u202f%]|dhs?$|mad$|dirhams?$", "", str(value).lower())
    if "," in text and "." in text:  # the last one is the decimal point: "1.005,50", "1,005.50"
        text = text.replace("." if text.rfind(",") > text.rfind(".") else ",", "")
    if "," in text:
        text = text.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", text):  # "1.005" or "12.500.000": thousands
        text = text.replace(".", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        raise ValueError(f"not a number: {value!r}")
    return float(text)


WRITTEN = re.compile(r"\d{1,3}(?:[ \u00a0\u202f.]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?")


def written_numbers(text):
    """Every number in a text, as numbers: "capital 100 000,50 DH" -> [100000.5]."""
    return [as_number(found) for found in WRITTEN.findall(text or "")]
