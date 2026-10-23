"""The real model shown Tarik as the agent has it (engine/agent.py): its system prompt, the skill the agent would
load, and its tools. It answers a conversation with a tool call, which is only read: no tool runs, no Experio.

One difference with the agent, kept small: the skill is in the system prompt (the agent loads it on demand). With no
skill (None), the model gets the agent's very own start: its system prompt, its list of skills and load_skill. The
model thinks as the agent's does (a 1024-token budget), unless THINKING=off (env). Each case runs REPEAT times (env,
default 1), 3 at once. Slow and billed: python -m pytest -m llm -s."""

import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from langchain_anthropic.chat_models import convert_to_anthropic_tool

from engine.tools.core.cabinets import setting, using
from engine.tools.core.model import MODEL_URL, model_name

CABINET = os.getenv("TARIK_TEST_CABINET", "experio_cabinet_8")
REPEAT = int(os.getenv("REPEAT", "1"))
THINKING = os.getenv("THINKING") != "off"


def agent():
    """The agent's system prompt without its list of skills (the one a case needs is in it, loaded), its skills by
    name, and its tools with their latest descriptions, load_skill aside: as the agent has them once the skill is
    loaded, so the first tool called is the decision measured."""
    from engine import agent as tarik

    skills = {name: instructions for name, _, instructions in tarik.skills()}
    rules = f"<built_in_rules>\n- {tarik.UNTRUSTED_RULE}\n</built_in_rules>"
    tools = [convert_to_anthropic_tool(tool) for tool in tarik.described() if tool.name != "load_skill"]
    start = tarik.system_prompt(), [convert_to_anthropic_tool(tool) for tool in tarik.described()]
    return f"{rules}\n\n{tarik.AGENT['instructions']}", skills, tools, start


@pytest.fixture(scope="session")
def ask():
    """ask(skill, messages) -> the model's whole answer (its JSON: content blocks, usage) and the seconds it took."""
    instructions, skills, tools, (start, all_tools) = agent()
    with using(CABINET):
        key, url = setting("MODEL_API_KEY", ""), setting("MODEL_API_URL", MODEL_URL)
    if not key:
        pytest.skip("no MODEL_API_KEY in Vault secret/tarik/orchestrator")
    http = httpx.Client(timeout=180, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})

    def call(skill, messages):
        system = start if skill is None else f"{instructions}\n\nCompétence « {skill} », chargée :\n{skills[skill]}"
        request = {
            "model": model_name(),
            "max_tokens": 4096,
            "temperature": 0,
            "system": system,
            "tools": all_tools if skill is None else tools,
            "messages": messages,
        }
        if THINKING:  # as the agent sends it: no temperature, room for the thinking
            del request["temperature"]
            request |= {"max_tokens": 16384, "thinking": {"type": "enabled", "budget_tokens": 1024}}
        for attempt in range(6):  # the agents share this account: a 429 waits its turn, never crowds them out
            started = time.monotonic()
            answer = http.post(url.rstrip("/") + "/v1/messages", json=request)
            if answer.status_code not in (429, 529):  # 529: the provider overloaded for a moment, as busy
                break
            time.sleep(float(answer.headers.get("retry-after") or 2**attempt))
        answer.raise_for_status()
        return answer.json(), time.monotonic() - started

    return call


@pytest.fixture(scope="session")
def decide(ask):
    """decide(skill, messages) -> the first tool call the model makes ({"name", "input"}), or None. ``follow``: a
    load_skill is followed as the agent does, the call made with that skill loaded is the one returned."""

    def call(skill, messages, follow=False):
        content = ask(skill, messages)[0]["content"]
        first = next((block for block in content if block["type"] == "tool_use"), None)
        if follow and first and first["name"] == "load_skill":
            return call(first["input"].get("name"), messages)
        return first

    return call


@pytest.fixture(scope="session")
def measure(decide):
    """measure(cases, judge): each case ({skill, messages, expected, said, follow?: see decide}) run REPEAT times,
    judged by ``judge(call, expected)`` as "right", "asked again" (no decision: the client is asked again) or "wrong" (a
    decision the client did not make). Prints the cases missed and returns (accuracy, wrong rate, misses)."""

    def run(cases, judge):
        runs = [case for case in cases for _ in range(REPEAT)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            calls = list(pool.map(lambda case: decide(case["skill"], case["messages"], case.get("follow")), runs))
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
