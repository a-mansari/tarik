"""Checks the graphs against what they call and show: every tool a graph or the agent calls exists (tarik/), and
every step the canvas draws has its plain description (content/labels.json). Run in the container: python check.py
(exit 1 on a gap)."""

import ast
import sys
from pathlib import Path

from engine import agent, graphs, traitement
from engine.tools import TOOLS

ENGINE = Path(__file__).parent / "engine"
TOOL_ARGUMENT = {"step": 3, "call": 1, "run_tool": 1}  # where each caller takes the tool's name


def called():
    """{tool: where} for every tool named in the graphs' calls (step, call, run_tool) and the agent's read tools."""
    found = dict.fromkeys(traitement.CHAIN, "traitement.CHAIN")
    found |= dict.fromkeys(agent.READ_TOOLS, "content/agent.json read_tools")
    for path in ENGINE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in TOOL_ARGUMENT:
                at = TOOL_ARGUMENT[node.func.id]
                if len(node.args) > at and isinstance(node.args[at], ast.Constant):
                    found[node.args[at].value] = f"{path.name}:{node.lineno}"
    return found


def undescribed():
    return [
        f"{name}: no description for {node['id']}"
        for name in ("agent", *graphs.COMPILED)
        for node in graphs.shape(name)["nodes"]
        if node["kind"] != "end" and not node["description"]
    ]


if __name__ == "__main__":
    gaps = [f"{where}: no tool {name}" for name, where in called().items() if name not in TOOLS] + undescribed()
    print("\n".join(gaps) or f"ok: {len(called())} tools called, all exist; every step described")
    sys.exit(1 if gaps else 0)
