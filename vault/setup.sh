#!/bin/sh
# One-time setup of the local Vault: docker compose exec -T -e HOST_UID=$(id -u) vault sh /vault/config/setup.sh
#   - initialises it (one key share), keeps the unseal key and root token in vault/.local/unseal/
#   - enables the secrets store (kv v2 at secret/) and AppRole logins
#   - gives tarik-mcp a role that may only read secret/tarik/*, its ids in vault/.local/approle/
#   - gives tarik-settings (the Cabinets page) a role that may only read and write each cabinet's own settings,
#     secret/tarik/cabinets/*, its ids in vault/.local/settings-approle/
# Safe to run again: each step is skipped when already done.
set -e
export VAULT_ADDR=http://127.0.0.1:8200
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
# The Cabinets page edits each cabinet's own settings, nothing else: never the shared ones nor the orchestrator's.
path "secret/data/tarik/cabinets/*" { capabilities = ["read", "update"] }
POLICY
vault write auth/approle/role/tarik-settings token_policies=tarik-settings token_ttl=15m token_max_ttl=1h secret_id_ttl=0 >/dev/null
vault read -field=role_id auth/approle/role/tarik-settings/role-id > /vault/settings-approle/role_id
[ -s /vault/settings-approle/secret_id ] || vault write -f -field=secret_id auth/approle/role/tarik-settings/secret-id > /vault/settings-approle/secret_id
chmod 644 /vault/approle/role_id /vault/approle/secret_id /vault/settings-approle/role_id /vault/settings-approle/secret_id # read by the tarik containers' own user, through a read-only mount
[ -n "$HOST_UID" ] && chown -R "$HOST_UID" /vault/unseal /vault/approle /vault/settings-approle # owned by this machine's user, not root
echo "vault ready: kv at secret/, roles tarik-mcp and tarik-settings"
