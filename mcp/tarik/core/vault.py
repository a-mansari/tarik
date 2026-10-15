"""Tarik's settings in Vault (VAULT_ADDR): secret/tarik/shared holds the shared ones, and
secret/tarik/cabinets/<cabinet> each cabinet's own; the cabinets are those paths.

tarik-mcp logs in with its AppRole (role_id and secret_id files in /run/vault, vault/setup.sh), whose
policy only reads secret/tarik/*. Everything is read at start, then again every 5 minutes, so a
changed secret applies without a restart (a new cabinet still needs one: its schema is made at
start). When Vault can't be reached, the last values stay in use. Without VAULT_ADDR, nothing is
read here and the settings come from the environment: the tests only (server.py refuses to start)."""

import os
import threading
import time

import httpx

ADDRESS = os.getenv("VAULT_ADDR", "").rstrip("/")
ENABLED = bool(ADDRESS)
APPROLE = os.getenv("VAULT_APPROLE_DIR", "/run/vault")
REFRESH_SECONDS = 300
RETRY_SECONDS = 30

_lock = threading.Lock()
_state = {"token": "", "token_until": 0.0, "values": None, "next_read": 0.0}


def _request(method, path, token="", **kwargs):
    answer = httpx.request(method, ADDRESS + path, headers={"X-Vault-Token": token} if token else {}, timeout=10, **kwargs)
    if answer.status_code == 404:
        return None
    answer.raise_for_status()
    return answer.json()


def _login():
    ids = {name: open(os.path.join(APPROLE, name)).read().strip() for name in ("role_id", "secret_id")}
    auth = _request("POST", "/v1/auth/approle/login", json=ids)["auth"]
    _state.update(token=auth["client_token"], token_until=time.monotonic() + 0.8 * auth["lease_duration"])


def _read(method, path):
    if not _state["token"] or time.monotonic() > _state["token_until"]:
        _login()
    return _request(method, path, _state["token"])


def read_all():
    """Vault's settings: the shared ones, and each cabinet's own by cabinet."""
    shared = ((_read("GET", "/v1/secret/data/tarik/shared") or {}).get("data") or {}).get("data") or {}
    listed = ((_read("LIST", "/v1/secret/metadata/tarik/cabinets") or {}).get("data") or {}).get("keys") or []
    cabinets = {name: _read("GET", f"/v1/secret/data/tarik/cabinets/{name}")["data"]["data"]
                for name in listed if not name.endswith("/")}
    return shared, cabinets


def settings(wait=0):
    """The shared and per-cabinet settings, read again once they are 5 minutes old. ``wait``: seconds
    to keep trying a first read (Vault still starting or unsealing)."""
    with _lock:
        if _state["values"] is None or time.monotonic() >= _state["next_read"]:
            deadline = time.monotonic() + wait
            while True:
                try:
                    _state["values"] = read_all()
                    _state["next_read"] = time.monotonic() + REFRESH_SECONDS
                    break
                except Exception as error:
                    if _state["values"] is not None:  # Vault out for a while: carry on with what was read
                        print(f"vault: {type(error).__name__}; the last values stay in use", flush=True)
                        _state["next_read"] = time.monotonic() + RETRY_SECONDS
                        break
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
        return _state["values"]
