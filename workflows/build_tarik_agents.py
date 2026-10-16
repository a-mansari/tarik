"""Create or update Tarik, one native n8n agent per cabinet, from workflows/tarik-agent.json and
the skills in workflows/skills/ (one file each, the same skill in every agent).

Run on the host: python3 workflows/build_tarik_agents.py
Each cabinet's agent is "Tarik - <cabinet's name>", alike but for its cabinet: its MCP address
(/c/<cabinet>/mcp) and the fixed `cabinet` input of its workflow tools. An agent is found by that
address in its configuration, not by its name, so a name changed in n8n never makes it another
cabinet's. Through n8n's MCP server (N8N_MCP_API_URL in .env, its token in Vault): agents made
there are allowed in n8n MCP, which "Tarik - Emails" needs (ask_agent). An agent already published is
published again when its draft changed; one never published stays a draft. Prints each agent's id,
for AGENT_ID in its cabinet's Vault path (secret/tarik/cabinets/<cabinet>)."""

import functools
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


@functools.cache  # read when first needed: check_skills (tests/style) runs without .env or Vault
def settings():
    """N8N_MCP_API_URL from .env, N8N_MCP_API_TOKEN from the local Vault."""
    values = {}
    for line in (HERE.parent / ".env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            values[key.strip()] = value.strip()
    root = HERE.parent / "vault/.local/unseal/root_token"
    if "N8N_MCP_API_TOKEN" not in values and root.exists():
        request = urllib.request.Request(
            "http://127.0.0.1:8200/v1/secret/data/tarik/shared", headers={"X-Vault-Token": root.read_text().strip()}
        )
        with urllib.request.urlopen(request, timeout=10) as answer:
            values["N8N_MCP_API_TOKEN"] = json.loads(answer.read())["data"]["data"]["N8N_MCP_API_TOKEN"]
    return values


def n8n(tool, **arguments):
    """One tool of n8n's MCP server; its structured result."""
    request = urllib.request.Request(
        settings()["N8N_MCP_API_URL"],
        method="POST",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        ).encode(),
        headers={
            "authorization": f"Bearer {settings()['N8N_MCP_API_TOKEN']}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
    )
    result = json.loads(answer_text(request))["result"]
    content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    if result.get("isError") or content.get("ok") is False:
        raise SystemExit(f"{tool}: {content}")
    return content


def answer_text(request):
    """The JSON text n8n answers ``request`` (a urllib Request) with, out of its event stream when it streams."""
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
    return text


def config_for(cabinet, name):
    """The agent's configuration (dict) for ``cabinet`` (its database) named ``name``, from tarik-agent.json."""
    text = (HERE / "tarik-agent.json").read_text()
    return json.loads(text.replace("__CABINET_NAME__", name).replace("__CABINET__", cabinet))


def agents_by_cabinet():
    """The existing agents, by the cabinet of their MCP address: (id, get_agent's answer, project)."""
    found = {}
    for agent in n8n("search_agents")["data"]:
        config = n8n("get_agent", agentId=agent["id"])
        for server in (config.get("config") or config.get("agent", {}).get("config") or {}).get("mcpServers", []):
            for cabinet in CABINETS:
                if server.get("url", "").endswith(f"/c/{cabinet}/mcp"):
                    found.setdefault(cabinet, []).append((agent["id"], config, agent["projectId"]))
    return found


def skill_files():
    """The skills, written once in workflows/skills/*.md: front matter (name, description), then the
    instructions. n8n keeps skills per agent, so every agent gets the same copy of each."""
    for path in sorted((HERE / "skills").glob("*.md")):
        _, front, body = path.read_text().split("---", 2)
        meta = {
            key.strip(): value.strip() for key, _, value in (line.partition(":") for line in front.strip().splitlines())
        }
        yield {"name": meta["name"], "description": meta["description"], "instructions": body.strip()}


def unknown_names(skills, config):
    """The tool and field names a skill mentions that the agent does not have. A skill names tools as
    the model sees them (never a workflow's id or name): every tool starts with Experio_, the MCP
    server's own as n8n prefixes them, the workflows' as the template names them. A bare or renamed
    name would point the model at a tool that does not exist. Names are snake_case words."""
    known = {"load_skill"}
    for tool in config.get("tools", []):
        known |= {tool["name"], *tool.get("inputs", {})}
    for server in config.get("mcpServers", []):  # n8n shows an MCP server's tools as <server>_<tool>
        known |= {f"{server['name']}_{tool}" for tool in (server.get("toolFilter") or {}).get("tools", [])}
    names = r"\b[A-Za-z]+(?:_[a-z]+)+\b"  # snake_case, a tool's server prefix included (Experio_find_dossier)
    return {
        skill["name"]: sorted(set(re.findall(names, skill["instructions"] + " " + skill["description"])) - known)
        for skill in skills
    }


def check_skills():
    """Refuse to build when a skill, the instructions or a tool's description names a tool or field
    the agents do not have."""
    config = config_for("check", "check")
    texts = [
        *skill_files(),
        {
            "name": "instructions",
            "description": " ".join(tool["description"] for tool in config["tools"]),
            "instructions": config["instructions"],
        },
    ]
    wrong = {name: names for name, names in unknown_names(texts, config).items() if names}
    if wrong:
        raise SystemExit(f"skills name tools or fields the agents do not have: {wrong}")


def mutate(agent_id, base, operation):
    """One change to an agent whose configuration has hash ``base`` (n8n refuses a change made on an
    older one); the configuration's new hash. n8n's MCP server limits calls per minute: no call for nothing."""
    return n8n("mutate_agent", agentId=agent_id, baseConfigHash=base, operation=operation)["configHash"]


def sync_skills(agent_id, current, base):
    """Each skill of workflows/skills/ in the agent, as written there: updated in place (found by name),
    or created and attached, when it differs. A skill of the agent's not in workflows/skills/ is deleted.
    ``current``: get_agent's answer, ``base`` the hash since. Returns the skills' names and the new hash."""
    by_name = {skill["name"]: (skill_id, skill) for skill_id, skill in current["skills"].items()}
    wanted = list(skill_files())
    for skill in wanted:
        skill_id, now = by_name.get(skill["name"], (None, None))
        if now != skill:
            operation = {"type": "skill.upsert", "skill": skill, **({"skillId": skill_id} if skill_id else {})}
            base = mutate(agent_id, base, operation)
    for name, (skill_id, _) in by_name.items():
        if name not in {skill["name"] for skill in wanted}:
            base = mutate(agent_id, base, {"type": "skill.delete", "skillId": skill_id})
    return [skill["name"] for skill in wanted], base


def update(agent_id, config, current):
    """Replace agent ``agent_id``'s configuration (get_agent's answer ``current``) with ``config``, keeping
    the skills attached to it, when a value differs (n8n's editor re-saves it changed); the new hash."""
    config["skills"] = current["config"].get("skills") or []
    if all(current["config"].get(key) == value for key, value in config.items()):
        return current["configHash"]
    return mutate(agent_id, current["configHash"], {"type": "config.replace", "config": config})


def create(project, config):
    """A new agent in ``project`` with ``config`` (its name taken out of it); returns its id."""
    created = n8n("create_agent", projectId=project, name=config.pop("name"), config=config)
    return created.get("agentId") or created.get("id") or created["agent"]["id"]


def publish_if_published(agent_id, current, changed):
    """Publish the agent's draft when it has a published version that differs from it: this run ``changed``
    the draft, or it had changed since (``current``: get_agent's answer before this run). An agent never
    published stays a draft. Returns whether it was published."""
    agent = current["agent"]
    if not agent.get("activeVersionId") or not (changed or agent["versionId"] != agent["activeVersionId"]):
        return False
    n8n("publish_agent", agentId=agent_id)
    return True


def main():
    """Check the skills, then update each cabinet's agent (or create it) and its skills, publishing those already
    published; print each agent's id."""
    check_skills()
    existing = agents_by_cabinet()
    if any(len(agents) > 1 for agents in existing.values()):
        raise SystemExit(f"several agents for one cabinet, settle it in n8n first: {existing}")
    project = next(iter(existing.values()))[0][2] if existing else n8n("search_agents")["data"][0]["projectId"]
    for cabinet, name in CABINETS.items():
        config = config_for(cabinet, name)
        if cabinet in existing:
            agent_id, current, _ = existing[cabinet][0]
            base = update(agent_id, config, current)
        else:
            agent_id = create(project, config)
            current = n8n("get_agent", agentId=agent_id)
            base = current["configHash"]
        skills, final = sync_skills(agent_id, current, base)
        skills = ", ".join(skills)
        action = "created" if cabinet not in existing else "updated" if final != current["configHash"] else "unchanged"
        if cabinet in existing and publish_if_published(agent_id, current, action == "updated"):
            action += " and published"
        label = config.get("name", "Tarik - " + name)  # a created agent's name was taken out of its config
        print(f"{cabinet}: AGENT_ID={agent_id}  # {label}, {action}, skills: {skills}")  # for its Vault path


if __name__ == "__main__":
    sys.exit(main())
