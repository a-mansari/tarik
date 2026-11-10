"""The web app built on its design system (server/web/src/design/): no page picks a colour, a radius, a shadow or a
text size of its own, writes its own focus ring, uses an old class name, or draws a button the system does not."""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "server" / "web" / "src"
PAGES = [p for p in SRC.rglob("*") if p.suffix in (".css", ".vue") and "design" not in p.relative_to(SRC).parts]
OLD_CLASSES = ("primary", "panel-button", "card-action", "icon-button")
CUSTOM_BUTTONS = (
    "App.vue",
    "RoutineForm.vue",
    "ContentImport.vue",
)  # sidebar, segmented choices, a slot passed a class
BUTTON = re.compile(r'<button\b(?:[^>"]|"[^"]*")*>')


def styles(path):
    """Return the CSS of ``path``: the file itself, or a Vue file's <style> blocks."""
    text = path.read_text()
    return text if path.suffix == ".css" else "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S))


def found(pattern, text_of=styles):
    """Return 'file: line' for each match of ``pattern`` in the pages."""
    return [f"{p.relative_to(SRC)}: {m.group(0)[:70]}" for p in PAGES for m in re.finditer(pattern, text_of(p))]


@pytest.mark.style
@pytest.mark.parametrize(
    ("what", "pattern"),
    [
        ("a colour", r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\("),
        ("a radius", r"border-radius: (?!var\()[^;}\n]*(?:px|%)"),
        ("a shadow", r"box-shadow: (?!var\(|none)[^;}\n]*"),
        ("a text size", r"font-size: [0-9.]+rem"),
        ("a focus ring", r"outline: [0-9.]+px solid"),
    ],
)
def test_a_page_takes_its_values_from_the_design_tokens(what, pattern):
    assert not found(pattern), f"{what} written in a page: use a token from design/tokens.css\n" + "\n".join(
        found(pattern)
    )


@pytest.mark.style
def test_no_page_uses_a_class_the_design_system_replaced():
    names = "|".join(OLD_CLASSES)
    pattern = rf'class="[^"]*(?<![\w-])(?:{names})(?![\w-])|panel (?:bad|accent)(?![\w-])'
    assert not found(pattern, lambda p: p.read_text() if p.suffix == ".vue" else ""), "use .btn-*, .panel-*"


@pytest.mark.style
def test_every_button_is_a_design_system_button():
    plain = [
        f"{p.relative_to(SRC)}: {tag.group(0)[:80]}"
        for p in PAGES
        if p.suffix == ".vue" and p.name not in CUSTOM_BUTTONS
        for tag in BUTTON.finditer(p.read_text())
        if "btn" not in tag.group(0) and "to-top" not in tag.group(0) and "unread-count" not in tag.group(0)
    ]
    assert not plain, "a button without .btn (add it, or list the custom component):\n" + "\n".join(plain)
