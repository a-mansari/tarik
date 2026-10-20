"""Checks the graphs and the content against each other: every tool a graph or the agent calls exists (tarik/), the
stack's content has every document the code reads (each tool's prompt, each tool's description), every step the
canvas draws has its plain description, and every input a workflow is given its name (labels.json) and a place in
the workflow's INPUTS. Run in the container: python check.py (exit 1 on a gap)."""

import ast
import asyncio
import inspect
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from engine import agent, audit, chat, content, dossier, graphs, traitement
from engine.content import AGENT, LABELS
from engine.tools import TOOLS
from tarik.invoices.pipeline import INVOICE_CHAIN
from ui.pages import HIDDEN

ENGINE = Path(__file__).parent / "engine"
TOOL_ARGUMENT = {"step": 3, "run_step": 1, "call": 1, "run_tool": 1}  # where each caller takes the tool's name


def called():
    """{tool: where} for every tool named in the graphs' calls (step, run_step, call, run_tool) and the agent's read
    tools."""
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


def unnamed():
    """Every input a workflow is given has a name in plain words (labels.json: inputs), but those never shown."""
    discussion = set(chat.Chat.__annotations__) - set(chat.Steps.__annotations__)  # its inputs, not its outputs
    given = {*traitement.INPUTS, *dossier.INPUTS, *discussion, "sender", "subject", "files"}
    found = [f"input {key}: no name in labels.json" for key in sorted(given - HIDDEN - set(LABELS["inputs"]))]
    return found + [
        f"journal action {name}: no words in labels.json" for name in audit.ACTIONS if name not in LABELS["audit"]
    ]


def dropped():
    """Every argument an agent's workflow tool takes reaches its workflow, and is one the workflow keeps (its INPUTS):
    engine.tools.started keeps only those, any other is dropped without a word. Each tool is called with its
    run_workflow replaced, to see what it hands over."""
    kept = {"traitement": traitement.INPUTS, "dossier": dossier.INPUTS}
    handed, runtime = [], SimpleNamespace(state={"messages": []}, config={"configurable": {}})

    async def handing(name, runtime, inputs):
        handed.append((name, {**inputs, **agent.fixed(runtime)}))

    gaps, run_workflow = [], agent.run_workflow
    agent.run_workflow = handing
    try:
        for tool in agent.TOOLS:
            if not tool.coroutine or tool.coroutine.__name__ in agent.READ_TOOLS:
                continue  # load_skill, and the read tools: no workflow
            parameters = inspect.signature(tool.coroutine).parameters
            arguments = {n: 0 if p.default is p.empty else p.default for n, p in parameters.items() if n != "runtime"}
            asyncio.run(tool.coroutine(runtime=runtime, **arguments))
            name, inputs = handed[-1]
            gaps += [f"{tool.name}: {key} not handed to {name}" for key in arguments if key not in inputs]
            gaps += [f"{tool.name}: {key} not in {name}.INPUTS" for key in inputs if key not in kept[name]]
    finally:
        agent.run_workflow = run_workflow
    return gaps


if __name__ == "__main__":
    content.load()
    gaps = [f"{where}: no tool {name}" for name, where in called().items() if name not in TOOLS]
    gaps += missing_content() + undescribed() + unnamed() + dropped()
    print(
        "\n".join(gaps)
        or f"ok: {len(called())} tools called, all exist; the content has all it needs; every step described, "
        "every input named and kept"
    )
    sys.exit(1 if gaps else 0)
