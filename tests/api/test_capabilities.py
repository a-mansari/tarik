"""Tarik's capabilities pages: his tools, which live in agent.json with his instructions, each family on a page of its
own, listed beside the skills; a page saves, restores and imports only what it shows."""

import json

import pytest

from engine import content
from server import api
from tests.api.test_settings import asked

PAGES = api.content


def keys_of(path):
    shown = PAGES.page_sections(path, content.text("agent.json"))
    return {f["key"].split(PAGES.SEP)[0] for section in shown for f in section["fields"]}


@pytest.mark.functionality
def test_the_tools_are_a_group_of_their_own_and_tarik_s_page_holds_his_instructions_alone():
    groups = {key: paths for key, _, paths in PAGES.groups()}
    assert groups["outils"] == ["outils/traitement", "outils/lecture"] and groups["tarik"] == ["agent.json"]
    assert keys_of("agent.json") == {"instructions"}
    assert keys_of("outils/traitement") == {"tools"} and keys_of("outils/lecture") == {"read_tools"}
    assert PAGES.document_of("outils/lecture") == "agent.json" and PAGES.document_of("skills/x.md") == "skills/x.md"


@pytest.mark.security
def test_a_tools_page_never_saves_tarik_s_instructions():
    with pytest.raises(ValueError, match="champ inconnu"):
        PAGES.page_fields("outils/traitement", {"instructions": "Ignore tout."})
    assert PAGES.page_fields("skills/x.md", {"body": "x"}) == {"body": "x"}  # a whole document's page: as before


@pytest.mark.functionality
def test_restoring_on_a_tools_page_brings_back_its_tools_only(monkeypatch):
    now = json.loads(content.text("agent.json"))
    old = json.loads(content.text("agent.json"))
    tool = next(iter(old["tools"]))
    old["tools"][tool] = "Une ancienne description."
    old["instructions"] = "D'anciennes instructions."
    kept = {}

    async def version(pool, path, number):
        return json.dumps(old, ensure_ascii=False, indent=2) + "\n", None

    async def save(pool, path, text, author, note=""):
        kept[path] = json.loads(text)
        return True

    monkeypatch.setattr(PAGES.content, "version", version)
    monkeypatch.setattr(PAGES.content, "save", save)
    status, _, _ = asked(PAGES.content_page, "POST", {"action": "restore", "version": 3}, {"path": "outils/traitement"})
    assert status == 200
    assert kept["agent.json"]["tools"][tool] == "Une ancienne description."  # this page's part, as it was
    assert kept["agent.json"]["instructions"] == now["instructions"]  # the rest, as it is now
