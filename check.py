"""Checks the graphs and the content against each other: every tool a graph or the agent calls exists (tarik/), the
stack's content has every document the code reads (each tool's prompt, each tool's description), and every step the
canvas draws has its plain description (labels.json). Run in the container: python check.py (exit 1 on a gap)."""

import ast
import re
import sys
from pathlib import Path

from engine import agent, content, graphs
from engine.content import AGENT
from engine.tools import TOOLS
from tarik.invoices.pipeline import INVOICE_CHAIN

ENGINE = Path(__file__).parent / "engine"
TOOL_ARGUMENT = {"step": 3, "call": 1, "run_tool": 1}  # where each caller takes the tool's name


def called():
    """{tool: where} for every tool named in the graphs' calls (step, call, run_tool) and the agent's read tools."""
    found = dict.fromkeys(INVOICE_CHAIN, "pipeline.INVOICE_CHAIN")
    found |= dict.fromkeys(agent.READ_TOOLS, "agent.READ_TOOLS")
    for path in ENGINE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in TOOL_ARGUMENT:
                at = TOOL_ARGUMENT[node.func.id]
                if len(node.args) > at and isinstance(node.args[at], ast.Constant):
                    found[node.args[at].value] = f"{path.name}:{node.lineno}"
    return found


def missing_content():
    """What the code reads that the stack's content lacks: a tool's prompt, or a tool's description."""
    prompts = {
        name
        for path in Path("tarik").rglob("*.py")
        for name in re.findall(r'tool_prompt\("([a-z-]+)"\)', path.read_text())
    }
    gaps = [
        f"no content tools/{name}.md (read by tool_prompt)"
        for name in sorted(prompts)
        if f"tools/{name}.md" not in content.LATEST
    ]
    described = {**{f"Experio_{n}": v for n, v in AGENT.get("read_tools", {}).items()}, **AGENT.get("tools", {})}
    gaps += [
        f"agent.json describes no {tool.name}"
        for tool in agent.TOOLS
        if tool.name != "load_skill" and tool.name not in described
    ]
    return gaps


def undescribed():
    return [
        f"{name}: no description for {node['id']}"
        for name in ("agent", *graphs.COMPILED)
        for node in graphs.shape(name)["nodes"]
        if node["kind"] != "end" and not node["description"]
    ]


if __name__ == "__main__":
    content.load()
    gaps = [f"{where}: no tool {name}" for name, where in called().items() if name not in TOOLS]
    gaps += missing_content() + undescribed()
    print(
        "\n".join(gaps)
        or f"ok: {len(called())} tools called, all exist; the content has all it needs; every step described"
    )
    sys.exit(1 if gaps else 0)
