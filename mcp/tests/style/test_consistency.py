"""The code and the generated files kept consistent: one style (ruff's linter and formatter, as pyproject.toml
sets them), each workflow exactly what its script builds, and every tool a workflow, an agent or a skill names
really exists.

Needs workflows/, instructions/, docs/ and pyproject.toml next to mcp/ (tests/run.py mounts them in the container)."""

import json
import re
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
    "build_tarik_emails.py": "tarik-emails.json",
    "build_tarik_merge_test.py": "tarik-merge-test.json",
}

needs_workflows = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="workflows/ not mounted")


CONFIG = ROOT / "pyproject.toml"
CHECKED = [str(folder) for folder in (Path(__file__).resolve().parents[2], WORKFLOWS, ROOT / "docs") if folder.is_dir()]


def ruff(*command):
    """ruff's ``command`` on all of our Python code, in the repository's style."""
    found = [sys.executable, "-m", "ruff", *command, "--no-cache", "--config", str(CONFIG), *CHECKED]
    return subprocess.run(found, capture_output=True, text=True)


@pytest.mark.style
def test_no_lint_error():
    found = ruff("check", "--output-format", "concise")
    assert found.returncode == 0, found.stdout + found.stderr


@pytest.mark.style
def test_one_format():
    found = ruff("format", "--check")
    assert found.returncode == 0, f"run ruff format on the repository: {found.stdout}{found.stderr}"


@pytest.mark.style
@needs_workflows
@pytest.mark.parametrize("builder", BUILDERS)
def test_each_workflow_is_what_its_script_builds(builder, tmp_path):
    built = tmp_path / "workflow.json"
    subprocess.run([sys.executable, str(WORKFLOWS / builder), str(built)], check=True, capture_output=True)
    assert json.loads(built.read_text()) == json.loads((WORKFLOWS / BUILDERS[builder]).read_text()), (
        f"{BUILDERS[builder]} was edited by hand, or {builder} changed without rebuilding it"
    )


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


@pytest.mark.style
@needs_workflows
def test_every_caller_fills_every_input_of_the_workflow_it_calls():
    """The agents' workflow tools and the Execute Workflow nodes give each input of the called "Appel", no other."""
    workflows = {
        workflow["id"]: workflow
        for workflow in (json.loads(path.read_text()) for path in WORKFLOWS.glob("tarik-*.json"))
        if "nodes" in workflow
    }

    def inputs(workflow_id):
        trigger = next(node for node in workflows[workflow_id]["nodes"] if node["name"] == "Appel")
        return {value["name"] for value in trigger["parameters"].get("workflowInputs", {}).get("values", [])}

    agent = json.loads((WORKFLOWS / "tarik-agent.json").read_text())
    calls = [(tool["name"], tool["workflowId"], set(tool["inputs"])) for tool in agent["tools"]]
    calls += [
        (node["name"], node["parameters"]["workflowId"]["value"], set(node["parameters"]["workflowInputs"]["value"]))
        for workflow in workflows.values()
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.executeWorkflow"
    ]
    assert len(calls) == len(agent["tools"]) + 2  # the 2 follow-ups of "Tarik - Emails"
    for name, workflow_id, given in calls:
        assert given == inputs(workflow_id), name


@pytest.mark.style
def test_each_tool_prompt_is_read_by_the_tool_its_file_names():
    code = Path(__file__).resolve().parents[2] / "tarik"
    loaded = {  # prompt name -> the module loading it
        name: "mcp/" + str(path.relative_to(code.parent))  # mcp/ is /app in the container
        for path in code.rglob("*.py")
        for name in re.findall(r'instruction\("([a-z-]+)"\)', path.read_text())
    }
    files = {path.stem: path.read_text() for path in (ROOT / "instructions/tools").glob("*.md")}
    assert sorted(loaded) == sorted(files), "a prompt file no tool reads, or a tool reading a missing one"
    for name, text in files.items():
        assert f"({loaded[name]})" in text.split("\n---\n", 1)[0], f"{name}.md: used_by names another file"
