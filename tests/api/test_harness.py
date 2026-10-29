"""The dashboard's map of Tarik's harness: the channels requests come and go by, what Tarik reaches himself and where
each of his tools leads; a setting only as set or not, the routines named to admins only."""

import asyncio
import json

import pytest
from starlette.requests import Request

from engine import agent, content
from server import api


def harness(cabinet, role="member"):
    """The map as an account of ``role`` sees it for ``cabinet``: (its body, its nodes by id, its edges)."""
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"tarik_cabinet={cabinet}".encode())],
        "state": {"user": {"role": role}},
    }
    body = asyncio.run(api.harness.harness_page(Request(scope))).body.decode()
    found = json.loads(body)
    return body, {n["id"]: n for n in found["nodes"]}, {(e["source"], e["target"], e["kind"]) for e in found["edges"]}


@pytest.mark.functionality
@pytest.mark.security
def test_the_harness_map_links_tarik_to_each_tool_and_each_tool_to_where_it_acts(db_cabinet, monkeypatch):
    monkeypatch.setattr(api.runs, "CABINETS", {db_cabinet})  # the sidebar's choice
    monkeypatch.setenv(f"{db_cabinet.upper()}__IMAP_USER", "tarik-secret@example.com")
    body, nodes, edges = harness(db_cabinet)

    tools = [t.name for t in agent.TOOLS if t.name != "load_skill"]
    assert all(("tarik", f"tool:{name}", "uses") in edges for name in tools)  # each straight from Tarik
    assert all(("tarik", f"skill:{name}", "uses") in edges for name, _, _ in agent.skills())
    leads = {source: target for source, target, kind in edges if kind == "leads"}
    assert len(leads) == len(tools) and leads.pop("tool:Experio_consult_cgnc") == "rag"
    assert leads.pop("tool:Tarik_routine") == leads.pop("tool:Tarik_reglages") == "base"  # never Experio
    assert set(leads.values()) == {"experio"}  # every other tool acts in Experio
    family = {name: nodes[f"tool:{name}"]["group"] for name in tools}
    read = {f"Experio_{name}" for name in agent.READ_TOOLS}
    assert all(family[name] == "read" for name in read) and family["Tarik_routine"] == "own"
    assert all(family[name].startswith("workflow:") for name in tools if name not in read and "Tarik_" not in name)
    assert family["Experio_traiter_fichiers"] == "workflow:traitement"
    assert all(nodes[group]["kind"] == "group" for group in family.values())
    talks = {("clients", "email"), ("clients", "chat"), ("team", "email"), ("team", "chat")}  # only by these two
    talks |= {(channel, "conversations") for channel in ("email", "chat", "routine")} | {("conversations", "tarik")}
    assert all((a, b, "request") in edges and (b, a, "reply") in edges for a, b in talks)  # answered the same way back
    assert {(a, b) for a, b, kind in edges if b == "tarik"} == {("conversations", "tarik")}  # only in conversations
    shown = content.LABELS["graphs"]  # the Workflows page's names
    assert nodes["chat"]["name"] == shown["discussion"] and nodes["workflow:traitement"]["name"] == shown["traitement"]
    assert not nodes["email"]["off"] and "tarik-secret" not in body


@pytest.mark.security
def test_the_harness_map_names_the_routines_to_admins_only(db_cabinet, monkeypatch):
    monkeypatch.setattr(api.runs, "CABINETS", {db_cabinet})
    routines = [{"id": 3, "kind": "tva", "name": "Relance impayés client X"}]
    monkeypatch.setattr(api.harness, "routines_of_cabinet", lambda: routines)
    member = harness(db_cabinet)[1]["routine"]["description"]
    admin = harness(db_cabinet, "admin")[1]["routine"]["description"]
    assert "Relance impayés" not in member and member.endswith("Routines du cabinet : 1.")
    assert admin.endswith("Routines du cabinet : Relance impayés client X.")
