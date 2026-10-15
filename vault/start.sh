#!/bin/sh
# Start Vault, then unseal it with the local key (vault/.local/unseal/unseal_key, made by setup.sh).
# Local convenience only: on the public server the unseal key never sits next to Vault.
set -e
vault server -config=/vault/config/config.hcl &
server=$!
export VAULT_ADDR=http://127.0.0.1:8200
until vault status >/dev/null 2>&1 || [ $? -eq 2 ]; do sleep 1; done # 2: up, but sealed
if [ -f /vault/unseal/unseal_key ] && vault status 2>/dev/null | grep -q "Sealed *true"; then
  vault operator unseal "$(cat /vault/unseal/unseal_key)" >/dev/null && echo "vault unsealed"
fi
wait $server
