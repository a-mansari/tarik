#!/bin/sh
# Start Vault, then unseal it with the key setup.sh kept in /vault/unseal (vault/.local/unseal in development, the
# vault_keys volume in a deployment). The key sits on a volume only the Vault containers mount: whoever can read the
# host's volumes can unseal it. A hardened deployment unseals through a KMS instead (a `seal` stanza in config.hcl).
set -e
vault server -config=/vault/config/config.hcl &
server=$!
export VAULT_ADDR=http://127.0.0.1:8200
until vault status >/dev/null 2>&1 || [ $? -eq 2 ]; do sleep 1; done # 2: up, but sealed
if [ -f /vault/unseal/unseal_key ] && vault status 2>/dev/null | grep -q "Sealed *true"; then
  vault operator unseal "$(cat /vault/unseal/unseal_key)" >/dev/null && echo "vault unsealed"
fi
wait $server
