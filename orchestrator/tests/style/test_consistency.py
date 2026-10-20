"""The code and the content kept consistent: one style (ruff's linter and formatter, as pyproject.toml sets them),
the skills and instructions naming only the agent's tools, and each tool prompt read by the tool it names."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ORCHESTRATOR = Path(__file__).resolve().parents[2]
CONTENT = ORCHESTRATOR / "content"


def ruff(*command):
    """ruff's ``command`` on all of the orchestrator's Python code, in its style."""
    found = [sys.executable, "-m", "ruff", *command, "--no-cache", "--config", str(ORCHESTRATOR / "pyproject.toml")]
    return subprocess.run([*found, str(ORCHESTRATOR)], capture_output=True, text=True)


@pytest.mark.style
def test_no_lint_error():
    found = ruff("check", "--output-format", "concise")
    assert found.returncode == 0, found.stdout + found.stderr


@pytest.mark.style
def test_one_format():
    found = ruff("format", "--check")
    assert found.returncode == 0, f"run ruff format on orchestrator/: {found.stdout}{found.stderr}"


@pytest.mark.style
def test_skills_and_instructions_name_only_the_agents_tools():
    """A skill names tools as the model sees them (Experio_find_dossier), and their arguments: a bare or renamed name
    would point the model at a tool that does not exist. Names are snake_case words."""
    from engine import agent

    known = {name for tool in agent.TOOLS for name in (tool.name, *tool.args)}
    texts = {path.name: path.read_text() for path in (CONTENT / "skills").glob("*.md")}
    texts["instructions"] = agent.AGENT["instructions"] + " " + " ".join(agent.DESCRIPTIONS.values())
    names = r"\b[A-Za-z]+(?:_[a-z]+)+\b"
    wrong = {name: sorted(set(re.findall(names, text)) - known) for name, text in texts.items()}
    assert not {name: found for name, found in wrong.items() if found}


@pytest.mark.style
def test_each_tool_prompt_is_read_by_the_tool_its_file_names():
    code = ORCHESTRATOR / "tarik"
    loaded = {  # prompt name -> the module loading it
        name: str(path.relative_to(ORCHESTRATOR))
        for path in code.rglob("*.py")
        for name in re.findall(r'instruction\("([a-z-]+)"\)', path.read_text())
    }
    files = {path.stem: path.read_text() for path in (CONTENT / "tools").glob("*.md")}
    assert sorted(loaded) == sorted(files), "a prompt file no tool reads, or a tool reading a missing one"
    for name, text in files.items():
        assert f"({loaded[name]})" in text.split("\n---\n", 1)[0], f"{name}.md: used_by names another file"
