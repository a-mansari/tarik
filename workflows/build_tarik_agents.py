"""Create or update Tarik, one native n8n agent per cabinet, from workflows/tarik-agent.json.

Run on the host: python3 workflows/build_tarik_agents.py
Each cabinet's agent is "Tarik - <cabinet's name>", alike but for its cabinet: its MCP address
(/c/<cabinet>/mcp) and the fixed `cabinet` input of its workflow tools. An agent is found by that
address in its configuration, not by its name, so a name changed in n8n never makes it another
cabinet's. Through n8n's MCP server (N8N_MCP_API_URL in .env, its token in Vault): agents made
there are allowed in n8n MCP, which "Tarik - Emails" needs (ask_agent). Prints each agent's id,
for <CABINET>__AGENT_ID in .env."""

import json
import sys
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
    with urllib.request.urlopen(request, timeout=120) as answer:
        text = answer.read().decode()
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


def main():
    existing = agents_by_cabinet()
    if any(len(agents) > 1 for agents in existing.values()):
        raise SystemExit(f"several agents for one cabinet, settle it in n8n first: {existing}")
    project = next(iter(existing.values()))[0][2] if existing else n8n("search_agents")["data"][0]["projectId"]
    for cabinet, name in CABINETS.items():
        config = config_for(cabinet, name)
        if cabinet in existing:
            agent_id, config_hash, _ = existing[cabinet][0]
            n8n("mutate_agent", agentId=agent_id, baseConfigHash=config_hash, operation={"type": "config.replace", "config": config})
            action = "updated"
        else:
            created = n8n("create_agent", projectId=project, name=config.pop("name"), config=config)
            agent_id = created.get("agentId") or created.get("id") or created["agent"]["id"]
            action = "created"
        print(f"{cabinet.upper()}__AGENT_ID={agent_id}  # {config.get('name', 'Tarik - ' + name)}, {action}")


if __name__ == "__main__":
    sys.exit(main())
