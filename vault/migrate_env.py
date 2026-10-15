"""Move Tarik's settings from .env into the local Vault, once: python3 vault/migrate_env.py

The shared settings go to secret/tarik/shared, each cabinet's (TARIK_CABINETS, prefixed) to
secret/tarik/cabinets/<cabinet>. Which key is which is read from mcp/tarik/core/cabinets.py, so
the two never disagree. .env is copied to vault/.local/env.before-vault first, then loses the moved
lines. No value is printed. Uses the local root token (vault/.local/unseal/root_token, setup.sh)."""

import ast
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
VAULT = "http://127.0.0.1:8200"


def setting_names():
    """CABINET_SETTINGS and SHARED_SETTINGS, as cabinets.py defines them."""
    tree = ast.parse((ROOT / "mcp/tarik/core/cabinets.py").read_text())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id in ("CABINET_SETTINGS", "SHARED_SETTINGS"):
            found[node.targets[0].id] = set(ast.literal_eval(node.value.args[0]))
    return found["CABINET_SETTINGS"], found["SHARED_SETTINGS"]


def unquoted(value):
    """The value as Docker reads it from .env: without the quotes around it."""
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value


def write(path, data):
    token = (ROOT / "vault/.local/unseal/root_token").read_text().strip()
    request = urllib.request.Request(f"{VAULT}/v1/secret/data/tarik/{path}", method="POST", data=json.dumps({"data": data}).encode(),
                                     headers={"X-Vault-Token": token, "content-type": "application/json"})
    urllib.request.urlopen(request, timeout=10).read()


def main():
    own_keys, shared_keys = setting_names()
    lines = ENV.read_text().splitlines()
    values = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#") and value.strip():
            values[key.strip()] = unquoted(value.strip())
    cabinets = [name.strip() for name in values.get("TARIK_CABINETS", "").split(",") if name.strip()]
    if not cabinets:
        raise SystemExit("no TARIK_CABINETS in .env: nothing to move (already moved?)")
    shared = {key: value for key, value in values.items() if key in shared_keys}
    write("shared", shared)
    print(f"tarik/shared: {len(shared)} settings ({', '.join(sorted(shared))})")
    moved = {"TARIK_CABINETS", *shared}
    for name in cabinets:
        prefix = f"{name.upper()}__"
        own = {key[len(prefix):]: value for key, value in values.items() if key.startswith(prefix) and key[len(prefix):] in own_keys}
        write(f"cabinets/{name}", own)
        moved |= {prefix + key for key in own}
        print(f"tarik/cabinets/{name}: {len(own)} settings ({', '.join(sorted(own))})")
    backup = ROOT / "vault/.local/env.before-vault"
    shutil.copy2(ENV, backup)
    backup.chmod(0o600)
    kept = [line for line in lines if line.partition("=")[0].strip() not in moved]
    ENV.write_text("\n".join(kept) + "\n")
    print(f".env: {len(lines) - len(kept)} lines moved to Vault, copy of the old file in {backup.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
