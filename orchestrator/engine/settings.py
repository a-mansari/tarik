"""What this stack reads from Vault, with tarik-mcp's own AppRole (read-only on secret/tarik/*): the shared settings
(secret/tarik/shared) and its own (secret/tarik/langgraph: the model's address and key). Read once per process."""

import functools
import os
from pathlib import Path

import httpx

VAULT = os.environ["VAULT_ADDR"].rstrip("/")
APPROLE = Path("/run/vault")
DEFAULT_CABINET = os.getenv("TARIK_DEFAULT_CABINET", "experio_cabinet_8")
MCP = os.getenv("TARIK_MCP", "http://tarik-mcp:8000")
SKILLS = Path("/instructions/skills")


@functools.cache
def token():
    ids = {name: (APPROLE / name).read_text().strip() for name in ("role_id", "secret_id")}
    return httpx.post(f"{VAULT}/v1/auth/approle/login", json=ids, timeout=10).raise_for_status().json()["auth"]["client_token"]


def secret(path):
    """secret/tarik/<path> as a dict, {} when it does not exist."""
    answer = httpx.get(f"{VAULT}/v1/secret/data/tarik/{path}", headers={"X-Vault-Token": token()}, timeout=10)
    if answer.status_code == 404:
        return {}
    return answer.raise_for_status().json()["data"]["data"]


@functools.cache
def shared():
    """secret/tarik/shared."""
    return secret("shared")


@functools.cache
def own():
    """secret/tarik/langgraph: this stack's own settings (MODEL_API_URL, MODEL_API_KEY), {} until they are written."""
    return secret("langgraph")


def cabinet_of(config):
    """The cabinet a run works for: config.configurable.cabinet, else the default one."""
    return (config or {}).get("configurable", {}).get("cabinet") or DEFAULT_CABINET
