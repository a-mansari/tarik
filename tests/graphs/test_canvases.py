"""The canvases the UI draws (engine.graphs.shape): every workflow's every step is drawn, named and described, in its
part, and the workflows' agent tools hand each its own inputs."""

import asyncio
from types import SimpleNamespace

import pytest

from check import sample_arguments
from engine import agent, declarations, dossier, exports, graphs, traitement

WORKFLOWS = {"traitement": traitement, "dossier": dossier, "declarations": declarations, "export": exports}


@pytest.mark.communication
@pytest.mark.parametrize("name", [*graphs.COMPILED, "agent"])
def test_each_step_of_a_workflow_is_drawn_named_and_described(name):
    shape = graphs.shape(name)
    steps = [node for node in shape["nodes"] if node["kind"] == "step"]
    assert steps and shape["label"] != name  # its own name in the UI
    unnamed = [node["id"] for node in steps if not node["description"]]
    assert not unnamed, f"{name}: steps without description {unnamed}"
    ids = {node["id"] for node in shape["nodes"]}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in shape["edges"])
    assert all(node["parent"] in ids for node in shape["nodes"] if node["parent"])  # each part drawn as a group


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dossier", {"creation", "configuration", "fichiers", "ecritures", "banques", "finalisation", "etat"}),
        ("declarations", {"expediteur", "tva", "ras", "delais"}),
        ("export", {"expediteur", "export"}),
        ("traitement", {"fichiers", "dossier_import", "brouillons", "reponse"}),
        ("fil", {"suite_documents", "suite_dossiers", "reponse", "fin"}),
    ],
)
def test_the_split_steps_have_each_their_own_box(name, expected):
    drawn = {node["id"] for node in graphs.shape(name)["nodes"]}
    assert expected <= drawn


@pytest.mark.technical
def test_a_step_a_part_may_start_at_is_drawn_from_its_first_entry_only():
    edge = SimpleNamespace(conditional=True, source="fichier:__start__", target="fichier:facture")
    assert graphs.hidden(edge, {}, {"fichier:facture": 2})
    assert not graphs.hidden(edge, {}, {"fichier:facture": 1})
    unconditional = SimpleNamespace(conditional=False, source="a", target="b:__end__")
    assert not graphs.hidden(unconditional, {"a": 3}, {})


@pytest.mark.functionality
def test_each_workflow_tool_runs_its_own_workflow_with_its_inputs(monkeypatch):
    handed = []

    async def run_workflow(name, runtime, inputs):
        handed.append((name, inputs))
        return "fait"

    monkeypatch.setattr(agent, "run_workflow", run_workflow)
    runtime = SimpleNamespace(state={"messages": []}, config={"configurable": {}})
    tools = [tool for tool in agent.TOOLS if tool.coroutine and tool.coroutine.__name__ not in agent.READ_TOOLS]
    expected = {
        "declaration_tva": "declarations",
        "declaration_ras": "declarations",
        "declaration_delais": "declarations",
        "exporter": "export",
        "traiter_fichiers": "traitement",
        "repondre": "traitement",
    }
    for tool in tools:
        arguments = sample_arguments(tool.coroutine)  # as check.py hands them: defaults, else 0
        asyncio.run(tool.coroutine(runtime=runtime, **arguments))
        name, inputs = handed[-1]
        assert name == expected.get(tool.coroutine.__name__, "dossier"), tool.name
        kept = set(WORKFLOWS[name].INPUTS)
        assert set(arguments) <= set(inputs) and set(inputs) - {"mode"} <= kept | {"mode"}, tool.name
