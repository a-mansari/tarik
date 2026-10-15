# Local Vault for Tarik's secrets (docker-compose.yml). Data kept in the vault_data volume.
# On the public server, use a real Vault: TLS on, and its own unseal (operators' key shares or a KMS).
storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true # local only: Docker's network, and 127.0.0.1 on the host
}

disable_mlock = true
ui            = true
api_addr      = "http://vault:8200"
