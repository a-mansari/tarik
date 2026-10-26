"""Check the graphs and the content against each other; print each gap and exit 1 on any.

Checks that every tool a graph or the agent calls exists (engine/tools/), the stack's content has every document the
code reads (each tool's prompt, each agent tool's description), every step the canvas draws has a description, and
every input a workflow is given has a name in labels.json and a place in the workflow's INPUTS.
Run in the container, from the repo root: python check.py"""

import ast
import asyncio
import inspect
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from engine import agent, chat, content, declarations, dossier, exports, graphs, traitement
from engine.calls import TOOLS
from engine.content import AGENT, LABELS
from engine.tools.invoices.pipeline import INVOICE_CHAIN
from server import audit
from server.api.runs import HIDDEN

ENGINE = Path(__file__).parent / "engine"
TOOL_ARGUMENT = {"step": 3, "run_step": 1, "call": 1, "run_tool": 1}  # where each caller takes the tool's name


def called():
    """Return {tool: where} for every tool named as a constant in engine/*.py calls to step, run_step, call or
    run_tool, plus INVOICE_CHAIN and the agent's read tools."""
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
    """Return the gaps: tool prompts (tool_prompt("name")) with no tools/<name>.md, and agent tools agent.json does not
    describe."""
    prompts = {
        name
        for path in Path("engine/tools").rglob("*.py")
        for name in re.findall(r'tool_prompt\("([a-z-]+)"\)', path.read_text())
    }
    gaps = [
        f"no content tools/{name}.md (read by tool_prompt)"
        for name in sorted(prompts)
        if f"tools/{name}.md" not in content.LATEST
    ]
    described = {f"Experio_{name}": text for name, text in AGENT.get("read_tools", {}).items()}
    described.update(AGENT.get("tools", {}))
    gaps += [
        f"agent.json describes no {tool.name}"
        for tool in agent.TOOLS
        if tool.name != "load_skill" and tool.name not in described
    ]
    return gaps


def undescribed():
    """Return the canvas nodes (agent and every workflow, starts and ends aside) with no description in labels.json."""
    return [
        f"{name}: no description for {node['id']}"
        for name in ("agent", *graphs.COMPILED)
        for node in graphs.shape(name)["nodes"]
        if node["kind"] != "end" and not node["description"]
    ]


def unnamed():
    """Return the gaps: workflow inputs with no name in labels.json (inputs) except HIDDEN ones, and journal actions
    with no words (audit)."""
    discussion = set(chat.Chat.__annotations__) - set(chat.Steps.__annotations__)  # its inputs, not its outputs
    given = {*traitement.INPUTS, *dossier.INPUTS, *declarations.INPUTS, *exports.INPUTS, *discussion}
    given |= {"sender", "subject", "files"}
    found = [f"input {key}: no name in labels.json" for key in sorted(given - HIDDEN - set(LABELS["inputs"]))]
    return found + [
        f"journal action {name}: no words in labels.json" for name in audit.ACTIONS if name not in LABELS["audit"]
    ]


def dropped():
    """Return the gaps where an agent workflow tool's argument does not reach its workflow, or is not in that
    workflow's INPUTS (engine.calls.started silently drops any other key).

    Each tool is called with agent.run_workflow replaced, to see what it hands over."""
    kept = {"traitement": traitement.INPUTS, "dossier": dossier.INPUTS, "declarations": declarations.INPUTS}
    kept["export"] = exports.INPUTS
    handed = []
    runtime = SimpleNamespace(state={"messages": []}, config={"configurable": {}})

    async def handing(name, runtime, inputs):
        handed.append((name, {**inputs, **agent.fixed(runtime)}))

    gaps = []
    run_workflow = agent.run_workflow
    agent.run_workflow = handing
    try:
        for tool in agent.TOOLS:
            if not tool.coroutine or tool.coroutine.__name__ in agent.READ_TOOLS:
                continue  # load_skill, and the read tools: no workflow
            arguments = sample_arguments(tool.coroutine)
            asyncio.run(tool.coroutine(runtime=runtime, **arguments))
            name, inputs = handed[-1]
            gaps += [f"{tool.name}: {key} not handed to {name}" for key in arguments if key not in inputs]
            gaps += [f"{tool.name}: {key} not in {name}.INPUTS" for key in inputs if key not in kept[name]]
    finally:
        agent.run_workflow = run_workflow
    return gaps


def sample_arguments(coroutine):
    """Return {argument: its default, else 0} for every argument of a tool's ``coroutine`` except runtime."""
    arguments = {}
    for name, parameter in inspect.signature(coroutine).parameters.items():
        if name != "runtime":
            arguments[name] = 0 if parameter.default is parameter.empty else parameter.default
    return arguments


if __name__ == "__main__":
    content.load()
    gaps = [f"{where}: no tool {name}" for name, where in called().items() if name not in TOOLS]
    gaps += missing_content() + undescribed() + unnamed() + dropped()
    if gaps:
        print("\n".join(gaps))
        sys.exit(1)
    print(
        f"ok: {len(called())} tools called, all exist; the content has all it needs; every step described, "
        "every input named and kept"
    )
    sys.exit(0)
