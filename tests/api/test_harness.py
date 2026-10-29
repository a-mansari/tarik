"""The dashboard's map of Tarik's harness: the channels requests come and go by, what Tarik reaches himself and where
each of his tools leads; a setting only as set or not."""

import asyncio
import json

import pytest
from starlette.requests import Request

from engine import agent, content
from server import api


@pytest.mark.functionality
@pytest.mark.security
def test_the_harness_map_links_tarik_to_each_tool_and_each_tool_to_experio_by_family(db_cabinet, monkeypatch):
    monkeypatch.setattr(api.runs, "CABINETS", {db_cabinet})  # the sidebar's choice
    monkeypatch.setenv(f"{db_cabinet.upper()}__IMAP_USER", "tarik-secret@example.com")
    scope = {"type": "http", "headers": [(b"cookie", f"tarik_cabinet={db_cabinet}".encode())]}
    body = asyncio.run(api.harness.harness_page(Request(scope))).body.decode()
    found = json.loads(body)
    nodes = {n["id"]: n for n in found["nodes"]}
    edges = {(e["source"], e["target"], e["kind"]) for e in found["edges"]}

    tools = [t.name for t in agent.TOOLS if t.name != "load_skill"]
    assert all(("tarik", f"tool:{name}", "uses") in edges for name in tools)  # each straight from Tarik
    assert all(("tarik", f"skill:{name}", "uses") in edges for name, _, _ in agent.skills())
    leads = {source: target for source, target, kind in edges if kind == "leads"}
    assert len(leads) == len(tools) and leads.pop("tool:Experio_consult_cgnc") == "rag"
    assert set(leads.values()) == {"experio"}  # every other tool acts in Experio
    family = {name: nodes[f"tool:{name}"]["group"] for name in tools}
    assert family["Experio_traiter_fichiers"] == "workflow:traitement" and family["Experio_list_dossiers"] == "read"
    assert all(nodes[group]["kind"] == "group" for group in family.values())
    talks = {("clients", "email"), ("clients", "chat"), ("team", "email"), ("team", "chat")}  # only by these two
    talks |= {(channel, "conversations") for channel in ("email", "chat", "routine")} | {("conversations", "tarik")}
    assert all((a, b, "request") in edges and (b, a, "reply") in edges for a, b in talks)  # answered the same way back
    assert {(a, b) for a, b, kind in edges if b == "tarik"} == {("conversations", "tarik")}  # only in conversations
    shown = content.LABELS["graphs"]  # the Workflows page's names
    assert nodes["chat"]["name"] == shown["discussion"] and nodes["workflow:traitement"]["name"] == shown["traitement"]
    assert not nodes["email"]["off"] and "tarik-secret" not in body
