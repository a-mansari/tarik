"""The real model (Vault's MODEL_API_*, MiniMax-M3, the agents' own) shown Tarik as the agents have it: their
instructions, the skill the agent would load, and their tools (workflow tools from workflows/tarik-agent.json,
MCP tools from the server). It answers a conversation with a tool call, which is only read: no n8n, no Experio.

Differences with n8n's native agent, kept small: the skill is in the system prompt (n8n loads it on demand), and
the model thinks as n8n has it (its reasoning setting, whatever its level, sends M3 a 1024-token budget M3 does not
keep to), unless THINKING=off (env). Each case runs REPEAT times (env, default 1), 3 at once."""

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from server import server
from tarik.core.cabinets import setting, using

CABINET = os.getenv("TARIK_TEST_CABINET", "experio_cabinet_8")
REPEAT = int(os.getenv("REPEAT", "1"))
THINKING = os.getenv("THINKING") != "off"
WORKFLOWS = Path(__file__).resolve().parents[3] / "workflows"  # the repository on the host, / in the container
TYPES = {"string": "string", "number": "number", "boolean": "boolean"}


def agent():
    """The agent's instructions, its skills by name, and its tools as the model API takes them."""
    config = json.loads((WORKFLOWS / "tarik-agent.json").read_text())
    instructions = config["instructions"].replace("__CABINET_NAME__", "GECAFISC CONSEIL")
    skills = {}
    for path in (WORKFLOWS.parent / "instructions/skills").glob("*.md"):
        _, front, body = path.read_text().split("---", 2)
        skills[next(line[6:] for line in front.splitlines() if line.startswith("name: "))] = body.strip()
    triggers = {}  # workflow id -> its "Appel" inputs' types
    for path in WORKFLOWS.glob("tarik-*.json"):
        workflow = json.loads(path.read_text())
        for node in workflow.get("nodes", []):
            if node["name"] == "Appel" and "workflowInputs" in node["parameters"]:
                values = node["parameters"]["workflowInputs"]["values"]
                triggers[workflow["id"]] = {value["name"]: value["type"] for value in values}
    tools = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": {
                "type": "object",
                "properties": {
                    name: {"type": TYPES[triggers[tool["workflowId"]][name]]}
                    for name, given in tool["inputs"].items()
                    if given["mode"] == "ai"
                },
            },
        }
        for tool in config["tools"]
    ]
    allowed = config["mcpServers"][0]["toolFilter"]["tools"]
    tools += [
        {"name": f"Experio_{tool.name}", "description": tool.description, "input_schema": tool.input_schema}
        for tool in asyncio.run(server.list_tools())
        if tool.name in allowed
    ]
    return instructions, skills, tools


@pytest.fixture(scope="session")
def ask():
    """ask(skill, messages) -> the model's whole answer (its JSON: content blocks, usage) and the seconds it took."""
    instructions, skills, tools = agent()
    with using(CABINET):
        url, key, model = (setting(name, "") for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_API_NAME"))
    if not (url and key and model):
        pytest.skip("no model in Vault (MODEL_API_*)")
    http = httpx.Client(timeout=180, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})

    def call(skill, messages):
        system = f"{instructions}\n\nCompétence « {skill} », chargée :\n{skills[skill]}"
        request = {
            "model": model,
            "max_tokens": 4096,
            "temperature": 0,
            "system": system,
            "tools": tools,
            "messages": messages,
        }
        if THINKING:  # as n8n sends it: no temperature, room for the thinking
            del request["temperature"]
            request |= {"max_tokens": 16384, "thinking": {"type": "enabled", "budget_tokens": 1024}}
        for attempt in range(6):  # the agents share this account: a 429 waits its turn, never crowds them out
            started = time.monotonic()
            answer = http.post(f"{url.rstrip('/')}/v1/messages", json=request)
            if answer.status_code != 429:
                break
            time.sleep(float(answer.headers.get("retry-after") or 2**attempt))
        answer.raise_for_status()
        return answer.json(), time.monotonic() - started

    return call


@pytest.fixture(scope="session")
def decide(ask):
    """decide(skill, messages) -> the first tool call the model makes ({"name", "input"}), or None."""

    def call(skill, messages):
        content = ask(skill, messages)[0]["content"]
        return next((block for block in content if block["type"] == "tool_use"), None)

    return call


@pytest.fixture(scope="session")
def measure(decide):
    """measure(cases, judge): each case ({skill, messages, expected, said}) run REPEAT times, judged by
    ``judge(call, expected)`` as "right", "asked again" (no decision: the client is asked again) or "wrong" (a
    decision the client did not make). Prints the cases missed and returns (accuracy, wrong rate, misses)."""

    def run(cases, judge):
        runs = [case for case in cases for _ in range(REPEAT)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            calls = list(pool.map(lambda case: decide(case["skill"], case["messages"]), runs))
        verdicts = [(case, call, judge(call, case["expected"])) for case, call in zip(runs, calls, strict=True)]
        misses = [(case["said"], case["expected"], call and call["input"], verdict) for case, call, verdict in verdicts]
        misses = [miss for miss in misses if miss[3] != "right"]
        for said, expected, given, verdict in misses:
            print(f"{verdict}: « {said} » expected {expected!r}, got {given!r}")
        right = sum(verdict == "right" for *_, verdict in verdicts) / len(verdicts)
        wrong = sum(verdict == "wrong" for *_, verdict in verdicts) / len(verdicts)
        print(f"{len(verdicts)} decisions: {right:.0%} right, {wrong:.0%} wrong")
        return right, wrong, misses

    return run
