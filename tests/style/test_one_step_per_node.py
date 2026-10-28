"""Each workflow's canvas shows each step it runs: a node calls one step only (run_step, or a nested workflow), never
two, so the UI's drawing (engine.graphs, from the compiled graphs) is the run step for step."""

import ast
from pathlib import Path

import pytest

GRAPHS = ("traitement", "dossier", "declarations", "exports", "releves", "emails", "chat", "routine")


def steps_of(function):
    """The steps a node function calls: run_step / call / step's step names, and the workflows it runs nested."""
    found = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        texts = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if name in ("run_step", "call", "step") and texts:
            found.add(texts[0])
        if name in ("nested", "resumed") and texts:
            found.add(f"workflow {texts[0]}")
    return found


@pytest.mark.style
@pytest.mark.parametrize("module", GRAPHS)
def test_each_node_runs_one_step(module):
    tree = ast.parse(Path(f"engine/{module}.py").read_text())
    nodes = {n.name: n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
    several = {name: sorted(steps) for name, node in nodes.items() if len(steps := steps_of(node)) > 1}
    assert not several, f"engine/{module}.py: a node running several steps hides some from its canvas: {several}"
