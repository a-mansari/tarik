"""What the team edits in the UI (engine/content.py, server/api/content.py): a form sent back unchanged gives back the
document exactly, and a change that would break what reads it is refused, in plain French."""

import json
import re

import pytest

from engine import content
from server import api


def form_of(path):
    """The form's fields as the page shows them, as the page sends them back (JSON: key, text)."""
    sections = api.content.sections(path, content.text(path))
    return {f["key"]: f["value"] for section in sections for f in section["fields"]}


# A line that starts a block of its own: a list item, a heading, a table row, a quote, a fence, a JSON object, or a
# "field: ..." line. Anything else right after a non-empty line continues its sentence.
BLOCK = re.compile(r"\s*([-*•]\s|\d+[.)]\s|[A-Z][.)]\s|#|\||>|```|\{|[a-z_]+:\s)")


def unwrapped(text):
    """``text`` with the line breaks a fixed width put inside its sentences removed (they show as broken lines in a
    form): a line that is not a block of its own joins the one before, unless that one ends a block (``:``, ``}``)."""
    out, fence = [], False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = not fence
            out.append(line)
            continue
        previous = out[-1].rstrip() if out else ""
        if fence or not previous or not stripped or BLOCK.match(line) or previous.endswith(("}", "]", ":")):
            out.append(line)
        else:
            out[-1] = previous + " " + stripped
    return "\n".join(out)


@pytest.mark.functionality
@pytest.mark.parametrize("path", content.documents())
def test_a_form_sent_back_unchanged_is_the_same_document(path):
    assert api.content.rebuilt(path, form_of(path)) == content.text(path)
    content.checked(path, content.text(path))


@pytest.mark.functionality
def test_a_field_changed_changes_that_field_only():
    form = form_of("labels.json")
    form["steps|traitement|post_invoice"] = "Comptabilisation dans Experio"
    new = json.loads(api.content.rebuilt("labels.json", form))
    old = json.loads(content.text("labels.json"))
    old["steps"]["traitement"]["post_invoice"] = "Comptabilisation dans Experio"
    assert new == old


@pytest.mark.security
@pytest.mark.parametrize(
    "path, change, refused",
    [
        ("skills/traitement-documents.md", lambda t: t + "\nAppelle Experio_supprimer_tout.\n", "outils ou champs"),
        ("skills/traitement-documents.md", lambda t: t.replace("name: ", "name: Autre "), "en-tête"),
        ("tools/check-invoice.md", lambda t: t.split("\n---\n")[0] + "\n---\n  \n", "vide"),
        ("agent.json", lambda t: t.replace('"model": "', '"model": "autre-'), "ne se modifie pas"),
        ("agent.json", lambda t: json.dumps({**json.loads(t), "en_plus": "x"}), "rubriques"),
        ("labels.json", lambda t: t[:-3], "JSON"),
    ],
)
def test_a_change_that_would_break_its_reader_is_refused(path, change, refused):
    with pytest.raises(ValueError, match=refused):
        content.checked(path, change(content.text(path)))


@pytest.mark.style
@pytest.mark.parametrize("path", content.documents())
def test_no_sentence_is_cut_by_a_line_break(path):
    """Text pasted from a file wrapped at a fixed width shows as broken lines in the form: each sentence on one line."""
    text = content.text(path)
    if path.endswith(".json"):
        data = json.loads(text)
        texts = [v for group in ("tools", "read_tools") for v in data.get(group, {}).values()]
        texts += [data["instructions"]] if "instructions" in data else []
    else:
        texts = [content.front_matter(text)[1]]
    for found in texts:
        assert unwrapped(found) == found


@pytest.mark.communication
def test_a_versions_change_is_shown_word_by_word_with_little_around_it():
    before = " ".join(f"mot{i}" for i in range(100)) + " le compte est 6145 puis la suite " + " ".join("x" * 40)
    after = before.replace("6145", "6125")
    found = api.content.diff(before, after)
    assert ("-", "6145") in found and ("+", "6125") in found
    assert sum(len(part) for _, part in found) < len(before) / 2  # the long unchanged stretches are cut


@pytest.mark.functionality
def test_only_the_fields_a_version_changed_are_compared():
    form = form_of("labels.json")
    form["steps|traitement|post_invoice"] = "Comptabilisation dans Experio"
    after = api.content.rebuilt("labels.json", form)
    found = api.content.changes("labels.json", content.text("labels.json"), after)
    assert list(found) == ["steps|traitement|post_invoice"]
    assert api.content.changes("labels.json", None, after) == {}  # the first version: nothing to compare


@pytest.mark.security
def test_a_new_document_from_an_import_is_checked_on_its_own():
    kept = content.LATEST.pop("agent.json")  # as in a new stack, without one yet
    try:
        with pytest.raises(ValueError, match="manque"):
            content.checked("agent.json", '{"instructions": "x"}')
        content.checked("agent.json", kept)  # a whole agent.json is taken
    finally:
        content.LATEST["agent.json"] = kept
    assert content.PATH.fullmatch("tools/read-new-answer.md")
    assert not content.PATH.fullmatch("../secrets.md") and not content.PATH.fullmatch("tools/x.py")


@pytest.mark.security
def test_an_import_may_bring_a_new_tools_description_but_never_a_tool_this_stack_lacks():
    agent = json.loads(content.text("agent.json"))
    agent["read_tools"]["new_tool"] = "x"
    content.checked("agent.json", json.dumps(agent), reshape=True)
    agent["instructions"] += " Appelle Experio_supprimer_tout."
    with pytest.raises(ValueError, match="outils ou champs"):
        content.checked("agent.json", json.dumps(agent), reshape=True)


@pytest.mark.security
def test_a_form_that_is_not_fields_of_text_is_refused():
    form = form_of("labels.json")
    for bad in (None, [], {**form, "statuses|done": {"x": 1}}, {f"statuses|{i}": "x" for i in range(501)}):
        with pytest.raises(ValueError):
            api.content.rebuilt("labels.json", bad)
    with pytest.raises(ValueError, match="champ inconnu"):
        api.content.rebuilt("labels.json", {**form, "statuses|en_plus": "x"})
