# Vault seed files

Copy this folder to `deploy/seed/` (ignored by git, it holds secrets), fill it, and give its path as `TARIK_SEED_DIR`
in `.env`. The `vault-init` container loads each file into Vault when its path does not exist yet:

| File | Vault path |
|---|---|
| `orchestrator.json` | `secret/tarik/orchestrator` |
| `shared.json` | `secret/tarik/shared` |
| `cabinets/<cabinet>.json` | `secret/tarik/cabinets/<cabinet>` |

Name each cabinet's file with the cabinet's **exact Experio database name**, and drop the example's placeholder
file. At least one cabinet is needed: the server does not start without. The keys are those of `.env.example`'s last
block. Values are JSON strings. Every `CHANGE_ME` must be replaced or its line deleted.

Later changes go through the Experio API or the Cabinets page (they are kept: a path already in Vault is never
overwritten). To push a changed file anyway, run the stack once with `VAULT_SEED_OVERWRITE=1`.
Delete the seed folder from the machine once Vault holds the values, if you prefer: Vault is then the only copy.
