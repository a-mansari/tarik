"""Parity check: every tarik-mcp tool the n8n workflows call is called by the graphs, and no other (an "Appel"
input the agents fill is passed on too). Run in the container: python check.py (exit 1 on a gap)."""

import json
import re
import sys
from pathlib import Path

GRAPHS = Path(__file__).parent / "engine"
WORKFLOWS = Path("/workflows")


def n8n_tools():
    found = set()
    for path in WORKFLOWS.glob("tarik-*.json"):
        if "merge-test" in path.name or "nodes" not in json.loads(path.read_text()):
            continue
        for node in json.loads(path.read_text())["nodes"]:
            if node["type"].endswith("mcpClient"):
                found.add(node["parameters"]["tool"]["value"])
    return found


def graph_tools():
    source = "".join(path.read_text() for path in GRAPHS.glob("*.py"))
    # Every quoted snake_case word of the graphs' source that names a tool n8n's workflows call. The agent's read tools
    # (experio_connection, list_dossiers...) are not workflow steps: both agents take them from the same template.
    # ask_agent (n8n's call_agent in between) has no counterpart: the Emails graph runs the agent graph itself.
    return (set(re.findall(r'"([a-z_]+)"', source)) & n8n_tools()) | {"ask_agent"}


def inputs():
    """Each workflow's "Appel" inputs (n8n) against the graphs' INPUTS."""
    gaps = []
    for name, module in (("traitement", "traitement"), ("dossier", "dossier")):
        nodes = json.loads((WORKFLOWS / f"tarik-{name}.json").read_text())["nodes"]
        appel = next(node for node in nodes if node["name"] == "Appel")
        n8n = {value["name"] for value in appel["parameters"]["workflowInputs"]["values"]}
        source = (GRAPHS / f"{module}.py").read_text()
        ours = set(re.search(r'INPUTS = \(\n((?:.*\n)+?)\)\.split\(\)', source).group(1).replace('"', "").split())
        if n8n != ours:
            gaps.append(f"{name} inputs: n8n only {sorted(n8n - ours)}, graph only {sorted(ours - n8n)}")
    return gaps


if __name__ == "__main__":
    n8n, ours = n8n_tools(), graph_tools()
    gaps = inputs()
    if n8n - ours:
        gaps.append(f"tools n8n calls, the graphs don't: {sorted(n8n - ours)}")
    if ours - n8n:
        gaps.append(f"tools the graphs call, n8n doesn't: {sorted(ours - n8n)}")
    print("\n".join(gaps) or f"parity: {len(n8n)} tools, same inputs")
    sys.exit(1 if gaps else 0)
