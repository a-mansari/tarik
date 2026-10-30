# Vault for Tarik's secrets (docker-compose.yml, docker-compose.prod.yml). Data kept in the vault_data volume.
# It listens on the compose network only (no published port), in plain HTTP: put TLS on and a KMS seal in front of
# anything reachable from outside that network.
storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true # the compose network only, never published
}

disable_mlock = true
ui            = true
api_addr      = "http://vault:8200"
