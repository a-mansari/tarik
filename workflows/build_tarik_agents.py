"""Create or update Tarik, one native n8n agent per cabinet, from workflows/tarik-agent.json and
the skills in workflows/skills/ (one file each, the same skill in every agent).

Run on the host: python3 workflows/build_tarik_agents.py
Each cabinet's agent is "Tarik - <cabinet's name>", alike but for its cabinet: its MCP address
(/c/<cabinet>/mcp) and the fixed `cabinet` input of its workflow tools. An agent is found by that
address in its configuration, not by its name, so a name changed in n8n never makes it another
cabinet's. Through n8n's MCP server (N8N_MCP_API_URL in .env, its token in Vault): agents made
there are allowed in n8n MCP, which "Tarik - Emails" needs (ask_agent). Prints each agent's id,
for <CABINET>__AGENT_ID in .env."""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
# Each cabinet's exact Experio database, and its name as the cabinet knows it.
CABINETS = {
    "experio_cabinet_8": "GECAFISC",
    "experio_cabinet_158": "BOURSE GESTION",
    "experio_cabinet_60": "KRESTON CONSULTING",
    "experio_cabinet_232": "CHAMS CONSULTING",
    "experio_cabinet_76": "Fiduciaire la référence",
    "experio_cabinet_383": "ABDELALI TIREGUIT",
    "experio_cabinet_451": "Ourtassi Audit",
    "experio_cabinet_64": "BIG ACCOUNTING LTD",
}


def env():
    """N8N_MCP_API_URL from .env, N8N_MCP_API_TOKEN from the local Vault (vault/migrate_env.py moved it there)."""
    values = {}
    for line in (HERE.parent / ".env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            values[key.strip()] = value.strip()
    root = HERE.parent / "vault/.local/unseal/root_token"
    if "N8N_MCP_API_TOKEN" not in values and root.exists():
        request = urllib.request.Request("http://127.0.0.1:8200/v1/secret/data/tarik/shared",
                                         headers={"X-Vault-Token": root.read_text().strip()})
        with urllib.request.urlopen(request, timeout=10) as answer:
            values["N8N_MCP_API_TOKEN"] = json.loads(answer.read())["data"]["data"]["N8N_MCP_API_TOKEN"]
    return values


SETTINGS = env()


def n8n(tool, **arguments):
    """One tool of n8n's MCP server; its structured result."""
    request = urllib.request.Request(SETTINGS["N8N_MCP_API_URL"], method="POST", data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}).encode(),
        headers={"authorization": f"Bearer {SETTINGS['N8N_MCP_API_TOKEN']}", "content-type": "application/json",
                 "accept": "application/json, text/event-stream"})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=120) as answer:
                text = answer.read().decode()
            break
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 7:  # n8n's MCP server limits calls per minute: wait and try again
                raise
            time.sleep(int(error.headers.get("retry-after") or 0) or 5 * (attempt + 1))
    if "data:" in text:
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    result = json.loads(text)["result"]
    content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    if result.get("isError") or content.get("ok") is False:
        raise SystemExit(f"{tool}: {content}")
    return content


def config_for(cabinet, name):
    text = (HERE / "tarik-agent.json").read_text()
    return json.loads(text.replace("__CABINET_NAME__", name).replace("__CABINET__", cabinet))


def agents_by_cabinet():
    """The existing agents, by the cabinet of their MCP address."""
    found = {}
    for agent in n8n("search_agents")["data"]:
        config = n8n("get_agent", agentId=agent["id"])
        for server in (config.get("config") or config.get("agent", {}).get("config") or {}).get("mcpServers", []):
            for cabinet in CABINETS:
                if server.get("url", "").endswith(f"/c/{cabinet}/mcp"):
                    found.setdefault(cabinet, []).append((agent["id"], config.get("configHash"), agent["projectId"]))
    return found


def skill_files():
    """The skills, written once in workflows/skills/*.md: front matter (name, description), then the
    instructions. n8n keeps skills per agent, so every agent gets the same copy of each."""
    for path in sorted((HERE / "skills").glob("*.md")):
        _, front, body = path.read_text().split("---", 2)
        meta = {key.strip(): value.strip() for key, _, value in (line.partition(":") for line in front.strip().splitlines())}
        yield {"name": meta["name"], "description": meta["description"], "instructions": body.strip()}


def unknown_names(skills, config):
    """The tool and field names a skill mentions that the agent does not have. A skill names tools by
    the agent's own tool names (never a workflow's id or name): one renamed in the template, but not in
    the skill, would point the model at a tool that no longer exists. Names are snake_case words."""
    known = {"load_skill"}
    for tool in config.get("tools", []):
        known |= {tool["name"], *tool.get("inputs", {})}
    for server in config.get("mcpServers", []):
        known |= set((server.get("toolFilter") or {}).get("tools", []))
    return {skill["name"]: sorted(set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", skill["instructions"] + " " + skill["description"])) - known)
            for skill in skills}


def check_skills():
    """Refuse to build when a skill names a tool or field the agents do not have."""
    wrong = {name: names for name, names in unknown_names(list(skill_files()), config_for("check", "check")).items() if names}
    if wrong:
        raise SystemExit(f"skills name tools or fields the agents do not have: {wrong}")


def mutate(agent_id, operation):
    """One change to an agent, on its latest configuration (n8n refuses a change made on an older one)."""
    return n8n("mutate_agent", agentId=agent_id, baseConfigHash=n8n("get_agent", agentId=agent_id)["configHash"], operation=operation)


def sync_skills(agent_id):
    """Each skill of workflows/skills/ in the agent, as written there: updated in place (found by name),
    or created and attached. A skill no longer in workflows/skills/ is deleted."""
    current = n8n("get_agent", agentId=agent_id)
    by_name = {skill["name"]: skill_id for skill_id, skill in current["skills"].items()}
    wanted = list(skill_files())
    for skill in wanted:
        skill_id = by_name.get(skill["name"])
        mutate(agent_id, {"type": "skill.upsert", "skill": skill, **({"skillId": skill_id} if skill_id else {})})
    for name, skill_id in by_name.items():
        if name not in {skill["name"] for skill in wanted}:
            mutate(agent_id, {"type": "skill.delete", "skillId": skill_id})
    return [skill["name"] for skill in wanted]


def main():
    check_skills()
    existing = agents_by_cabinet()
    if any(len(agents) > 1 for agents in existing.values()):
        raise SystemExit(f"several agents for one cabinet, settle it in n8n first: {existing}")
    project = next(iter(existing.values()))[0][2] if existing else n8n("search_agents")["data"][0]["projectId"]
    for cabinet, name in CABINETS.items():
        config = config_for(cabinet, name)
        if cabinet in existing:
            agent_id = existing[cabinet][0][0]
            config["skills"] = n8n("get_agent", agentId=agent_id)["config"].get("skills") or []  # attached: kept
            mutate(agent_id, {"type": "config.replace", "config": config})
            action = "updated"
        else:
            created = n8n("create_agent", projectId=project, name=config.pop("name"), config=config)
            agent_id = created.get("agentId") or created.get("id") or created["agent"]["id"]
            action = "created"
        skills = sync_skills(agent_id)
        print(f"{cabinet.upper()}__AGENT_ID={agent_id}  # {config.get('name', 'Tarik - ' + name)}, {action}, skills: {', '.join(skills)}")


if __name__ == "__main__":
    sys.exit(main())
