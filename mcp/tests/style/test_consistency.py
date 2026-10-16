"""The code and the generated files kept consistent: one format (black, lines of 120 at most), no unused import
nor unknown name, each workflow
exactly what its script builds, and every tool a workflow, an agent or a skill names really exists.

Needs workflows/ next to mcp/ (tests/run.py mounts it in the container)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from server import server  # its import registers every tool


ROOT = Path(__file__).resolve().parents[3]  # the repository on the host, / in the container
WORKFLOWS = ROOT / "workflows"
BUILDERS = {
    "build_tarik_traitement.py": "tarik-traitement.json",
    "build_tarik_dossier.py": "tarik-dossier.json",
    "build_tarik_modification.py": "tarik-modification.json",
    "build_tarik_emails.py": "tarik-emails.json",
    "build_tarik_merge_test.py": "tarik-merge-test.json",
}

needs_workflows = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="workflows/ not mounted")


CHECKED = [str(Path(__file__).resolve().parents[2])] + ([str(WORKFLOWS)] if WORKFLOWS.is_dir() else [])
LINE_LENGTH = 120  # as pyproject.toml says, which the container doesn't see


@pytest.mark.style
def test_no_unused_import_nor_unknown_name():
    found = subprocess.run([sys.executable, "-m", "pyflakes", *CHECKED], capture_output=True, text=True)
    assert found.stdout == "", found.stdout


@pytest.mark.style
def test_one_format():
    black = [sys.executable, "-m", "black", "--check", "--quiet", "-l", str(LINE_LENGTH), "-t", "py312", *CHECKED]
    found = subprocess.run(black, capture_output=True, text=True)
    assert found.returncode == 0, f"run black on the repository: {found.stderr}"
    long = [
        f"{path}:{number}"
        for folder in CHECKED
        for path in Path(folder).rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if len(line) > LINE_LENGTH
    ]
    assert not long, f"lines over {LINE_LENGTH} characters (black leaves long strings and comments): {long}"


@pytest.mark.style
@needs_workflows
@pytest.mark.parametrize("builder", BUILDERS)
def test_each_workflow_is_what_its_script_builds(builder, tmp_path):
    built = tmp_path / "workflow.json"
    subprocess.run([sys.executable, str(WORKFLOWS / builder), str(built)], check=True, capture_output=True)
    assert json.loads(built.read_text()) == json.loads(
        (WORKFLOWS / BUILDERS[builder]).read_text()
    ), f"{BUILDERS[builder]} was edited by hand, or {builder} changed without rebuilding it"


@pytest.mark.style
@needs_workflows
def test_skills_and_instructions_name_only_the_agents_tools():
    sys.path.insert(0, str(WORKFLOWS))
    try:
        import build_tarik_agents

        build_tarik_agents.check_skills()  # SystemExit naming the unknown names
    finally:
        sys.path.remove(str(WORKFLOWS))


@pytest.mark.functionality
@needs_workflows
def test_an_agent_already_published_is_published_again_when_its_draft_changed(monkeypatch):
    sys.path.insert(0, str(WORKFLOWS))
    try:
        import build_tarik_agents
    finally:
        sys.path.remove(str(WORKFLOWS))
    published = []
    monkeypatch.setattr(build_tarik_agents, "n8n", lambda tool, agentId: published.append(agentId))

    def agent(draft, active):
        return {"agent": {"versionId": draft, "activeVersionId": active}}

    publish = build_tarik_agents.publish_if_published
    assert publish("changed", agent("v1", "v1"), changed=True)  # this run changed it
    assert publish("behind", agent("v2", "v1"), changed=False)  # its draft had changed since
    assert not publish("same", agent("v1", "v1"), changed=False)
    assert not publish("draft", agent("v1", None), changed=True)  # never published: stays a draft
    assert published == ["changed", "behind"]


@pytest.mark.style
@needs_workflows
def test_every_tool_a_workflow_or_an_agent_calls_exists():
    tools = {tool.name for tool in server._tool_manager.list_tools()}
    workflows = {path.name: json.loads(path.read_text()) for path in WORKFLOWS.glob("tarik-*.json")}
    agent = workflows.pop("tarik-agent.json")
    called = {
        node["parameters"]["tool"]["value"]
        for workflow in workflows.values()
        for node in workflow["nodes"]
        if node["type"] == "@n8n/n8n-nodes-langchain.mcpClient"
    }
    allowed = {tool for mcp in agent["mcpServers"] for tool in mcp["toolFilter"]["tools"]}
    assert called | allowed <= tools, f"not on the server: {sorted((called | allowed) - tools)}"
    started = {tool["workflowId"] for tool in agent["tools"] if tool["type"] == "workflow"}
    assert started <= {workflow["id"] for workflow in workflows.values()}
