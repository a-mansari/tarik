#!/bin/sh
# One-time setup of the local Vault: docker compose exec -T -e HOST_UID=$(id -u) vault sh /vault/config/setup.sh
#   - initialises it (one key share), keeps the unseal key and root token in vault/.local/unseal/
#   - enables the secrets store (kv v2 at secret/) and AppRole logins
#   - gives tarik-mcp a role that may only read secret/tarik/*, its ids in vault/.local/approle/
#   - gives tarik-settings (the Cabinets page, the Experio API) a role that may only read, create and update each
#     cabinet's own settings, secret/tarik/cabinets/*, its ids in vault/.local/settings-approle/
#   - makes EXPERIO_API_KEY (secret/tarik/orchestrator) once, a random key the Experio app shows to the API
# Safe to run again: each step is skipped when already done. In a deployment it runs as the vault-init container
# (docker-compose.prod.yml), against the Vault container, at each start of the stack.
set -e
export VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}" # the vault-init container sets it to the Vault container's
umask 077
mkdir -p /vault/unseal /vault/approle /vault/settings-approle
if ! vault status 2>/dev/null | grep -q "Initialized *true"; then
  vault operator init -key-shares=1 -key-threshold=1 > /vault/unseal/init.txt
  awk '/^Unseal Key 1:/ {print $4}' /vault/unseal/init.txt > /vault/unseal/unseal_key
  awk '/^Initial Root Token:/ {print $4}' /vault/unseal/init.txt > /vault/unseal/root_token
  [ -s /vault/unseal/unseal_key ] && [ -s /vault/unseal/root_token ] || { echo "init output not understood"; exit 1; }
  echo "initialised"
fi
if vault status 2>/dev/null | grep -q "Sealed *true"; then
  vault operator unseal "$(cat /vault/unseal/unseal_key)" >/dev/null
fi
export VAULT_TOKEN="$(cat /vault/unseal/root_token)"
vault secrets list | grep -q '^secret/' || vault secrets enable -path=secret kv-v2
vault auth list | grep -q '^approle/' || vault auth enable approle
vault policy write tarik-mcp - <<'POLICY'
# tarik-mcp reads Tarik's settings, nothing else, and writes none.
path "secret/data/tarik/*"     { capabilities = ["read"] }
path "secret/metadata/tarik/*" { capabilities = ["list"] }
POLICY
vault write auth/approle/role/tarik-mcp token_policies=tarik-mcp token_ttl=1h token_max_ttl=24h secret_id_ttl=0 >/dev/null
vault read -field=role_id auth/approle/role/tarik-mcp/role-id > /vault/approle/role_id
[ -s /vault/approle/secret_id ] || vault write -f -field=secret_id auth/approle/role/tarik-mcp/secret-id > /vault/approle/secret_id
vault policy write tarik-settings - <<'POLICY'
# The Cabinets page edits each cabinet's own settings, and the Experio API creates a cabinet's path (never deletes
# one: a cabinet is deactivated, ACTIVE 0); nothing else, never the shared settings nor the orchestrator's.
path "secret/data/tarik/cabinets/*" { capabilities = ["create", "read", "update"] }
POLICY
vault write auth/approle/role/tarik-settings token_policies=tarik-settings token_ttl=15m token_max_ttl=1h secret_id_ttl=0 >/dev/null
vault read -field=role_id auth/approle/role/tarik-settings/role-id > /vault/settings-approle/role_id
[ -s /vault/settings-approle/secret_id ] || vault write -f -field=secret_id auth/approle/role/tarik-settings/secret-id > /vault/settings-approle/secret_id
chmod 644 /vault/approle/role_id /vault/approle/secret_id /vault/settings-approle/role_id /vault/settings-approle/secret_id # read by the tarik containers' own user, through a read-only mount
[ -n "$HOST_UID" ] && chown -R "$HOST_UID" /vault/unseal /vault/approle /vault/settings-approle # owned by this machine's user, not root
# The stack's own secrets, loaded from the seed files (JSON, one object of settings per path) in $SEED_DIR (/seed):
#   orchestrator.json -> secret/tarik/orchestrator   shared.json -> secret/tarik/shared
#   cabinets/<cabinet's exact Experio database>.json -> secret/tarik/cabinets/<cabinet>
# A path is written only when it does not exist yet, so what the Experio API or the Cabinets page changed later is kept;
# VAULT_SEED_OVERWRITE=1 merges the files' keys into the existing paths too (a rotated key). The server needs at least
# one cabinet in Vault to start, hence the cabinets' seed.
SEED_DIR="${SEED_DIR:-/seed}"
put_file() {
  [ -f "$2" ] || return 0
  if vault kv get "$1" >/dev/null 2>&1; then
    [ "$VAULT_SEED_OVERWRITE" = 1 ] || return 0
    vault kv patch "$1" @"$2" >/dev/null && echo "updated $1"
  else
    vault kv put "$1" @"$2" >/dev/null && echo "seeded $1"
  fi
}
put_file secret/tarik/orchestrator "$SEED_DIR/orchestrator.json"
put_file secret/tarik/shared "$SEED_DIR/shared.json"
for file in "$SEED_DIR"/cabinets/*.json; do
  [ -f "$file" ] && put_file "secret/tarik/cabinets/$(basename "$file" .json)" "$file"
done
# The Experio app's key, made here once inside Vault when no seed file holds it (never typed, never shown): its admin
# reads it from Vault, or the seed gives it (EXPERIO_API_KEY in orchestrator.json) when the Experio app made it.
if ! vault kv get -field=EXPERIO_API_KEY secret/tarik/orchestrator >/dev/null 2>&1; then
  key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if vault kv get secret/tarik/orchestrator >/dev/null 2>&1; then
    vault kv patch secret/tarik/orchestrator EXPERIO_API_KEY="$key" >/dev/null
  else
    vault kv put secret/tarik/orchestrator EXPERIO_API_KEY="$key" >/dev/null
  fi
  echo "EXPERIO_API_KEY made in secret/tarik/orchestrator"
fi
echo "vault ready: kv at secret/, roles tarik-mcp and tarik-settings"
