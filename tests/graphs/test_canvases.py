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
    assert graphs.hidden(edge, set(), {"fichier:facture": 2})
    assert not graphs.hidden(edge, set(), {"fichier:facture": 1})
    unconditional = SimpleNamespace(conditional=False, source="a", target="b:__end__")
    assert not graphs.hidden(unconditional, {("a", "b:__end__")}, {})


@pytest.mark.communication
@pytest.mark.parametrize(
    ("source", "target", "kept_off"),
    [
        ("fichier:facture:lecture:create_draft_invoice", "fichier:facture:lecture:__end__", True),  # a stop
        ("fichier:facture:lecture:correct_invoice", "fichier:facture:lecture:__end__", False),  # the phase's last step
        ("fichier:facture:comptabilisation:apply_credit_note", "fichier:facture:comptabilisation:__end__", False),
        ("fichier:facture:comptabilisation:__end__", "fichier:facture:__end__", False),  # the invoice's last phase
        ("fichier:facture:lecture:__end__", "fichier:facture:__end__", True),  # a phase stopped
        ("fichier:facture:__end__", "fichier:__end__", False),  # the invoice done: on to the file's end
        ("fichier:facture:__end__", "fichier:facture:__start__", False),  # the drafts Experio split off the bill
        ("fichier:pointage", "fichier:__end__", False),  # no posting asked: an ordinary end
        ("fichier:identification", "fichier:__end__", True),  # neither an invoice nor a statement
        ("fichiers", "resume", True),  # the files not listed
    ],
)
def test_only_the_ways_taken_when_a_step_failed_are_kept_off_the_canvas(source, target, kept_off):
    edges = {(e["source"], e["target"]): e["hidden"] for e in graphs.shape("traitement")["edges"]}
    assert edges[(source, target)] is kept_off


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


def reachable(edges, start):
    seen, todo = {start}, [start]
    while todo:
        node = todo.pop()
        for following in edges.get(node, ()):
            if following not in seen:
                seen.add(following)
                todo.append(following)
    return seen


@pytest.mark.communication
@pytest.mark.parametrize("name", [*graphs.COMPILED, "agent"])
def test_every_drawn_step_lies_on_a_drawn_way_from_the_start_to_the_end(name):
    shape = graphs.shape(name)
    forward, backward = {}, {}
    for edge in shape["edges"]:
        if not edge["hidden"]:
            forward.setdefault(edge["source"], set()).add(edge["target"])
            backward.setdefault(edge["target"], set()).add(edge["source"])
    steps = {node["id"] for node in shape["nodes"] if node["kind"] != "group"}
    from_start, to_end = reachable(forward, "__start__"), reachable(backward, "__end__")
    assert not steps - from_start, f"{name}: drawn with no way in {sorted(steps - from_start)}"
    assert not steps - to_end, f"{name}: drawn with no way out to the end {sorted(steps - to_end)}"
