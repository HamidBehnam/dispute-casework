data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "dcw" {
  name = "rg-dcw-eus2"
}

data "azurerm_cognitive_account" "foundry" {
  name                = "ai-dcw-eus2"
  resource_group_name = data.azurerm_resource_group.dcw.name
}

locals {
  hostname      = "${var.subdomain}.${var.domain}"
  backend_image = "${data.azurerm_container_registry.dcw.login_server}/dcw-backend:${var.image_tag}"
  cerbos_image  = "ghcr.io/cerbos/cerbos:0.55.0@sha256:4b9d3b58c4f11c1b8953bc798d8d086e64f276882253ab169625fddc7f432515"
}

resource "azurerm_log_analytics_workspace" "dcw" {
  name                = "log-dcw-eus2"
  location            = data.azurerm_resource_group.dcw.location
  resource_group_name = data.azurerm_resource_group.dcw.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.2
}

# Pulls images and reads Key Vault; never available to code (lifecycle None).
resource "azurerm_user_assigned_identity" "platform" {
  name                = "id-dcw-platform-eus2"
  location            = data.azurerm_resource_group.dcw.location
  resource_group_name = data.azurerm_resource_group.dcw.name
}

# The application's own identity (lifecycle Main): Foundry and Postgres sign-ins.
resource "azurerm_user_assigned_identity" "main" {
  name                = "id-dcw-main-eus2"
  location            = data.azurerm_resource_group.dcw.location
  resource_group_name = data.azurerm_resource_group.dcw.name
}

data "azurerm_container_registry" "dcw" {
  name                = "crdcweus2"
  resource_group_name = data.azurerm_resource_group.dcw.name
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = data.azurerm_container_registry.dcw.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.platform.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "foundry_user" {
  scope                = data.azurerm_cognitive_account.foundry.id
  role_definition_name = "Foundry User"
  principal_id         = azurerm_user_assigned_identity.main.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_key_vault" "dcw" {
  name                       = "kv-dcw-eus2"
  location                   = data.azurerm_resource_group.dcw.location
  resource_group_name        = data.azurerm_resource_group.dcw.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
}

resource "azurerm_role_assignment" "secrets_user" {
  scope                = azurerm_key_vault.dcw.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.platform.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "secrets_officer" {
  scope                = azurerm_key_vault.dcw.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "certificates_officer" {
  scope                = azurerm_key_vault.dcw.id
  role_definition_name = "Key Vault Certificates Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_key_vault_secret" "origin_header" {
  name             = "origin-header-secret"
  key_vault_id     = azurerm_key_vault.dcw.id
  value_wo         = var.origin_header_secret
  value_wo_version = 1

  depends_on = [azurerm_role_assignment.secrets_officer]
}

# The private key is generated inside the vault and never leaves it; the
# pending request's CSR is signed by Cloudflare's Origin CA and merged back.
resource "azurerm_key_vault_certificate" "origin" {
  name         = "origin-certificate"
  key_vault_id = azurerm_key_vault.dcw.id

  certificate_policy {
    issuer_parameters {
      name = "Unknown"
    }

    key_properties {
      exportable = true
      key_size   = 2048
      key_type   = "RSA"
      reuse_key  = false
    }

    secret_properties {
      content_type = "application/x-pkcs12"
    }

    x509_certificate_properties {
      subject            = "CN=${local.hostname}"
      key_usage          = ["digitalSignature", "keyEncipherment"]
      extended_key_usage = ["1.3.6.1.5.5.7.3.1"]
      validity_in_months = 180

      subject_alternative_names {
        dns_names = [local.hostname]
      }
    }
  }

  depends_on = [azurerm_role_assignment.certificates_officer]
}

resource "azurerm_container_app_environment" "dcw" {
  name                       = "cae-dcw-eus2"
  location                   = data.azurerm_resource_group.dcw.location
  resource_group_name        = data.azurerm_resource_group.dcw.name
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.dcw.id

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.platform.id]
  }
}

resource "azurerm_container_app" "dcw" {
  name                         = "ca-dcw-eus2"
  container_app_environment_id = azurerm_container_app_environment.dcw.id
  resource_group_name          = data.azurerm_resource_group.dcw.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.platform.id, azurerm_user_assigned_identity.main.id]
  }

  registry {
    server   = data.azurerm_container_registry.dcw.login_server
    identity = azurerm_user_assigned_identity.platform.id
  }

  secret {
    name                = "origin-header-secret"
    identity            = azurerm_user_assigned_identity.platform.id
    key_vault_secret_id = azurerm_key_vault_secret.origin_header.versionless_id
  }

  ingress {
    external_enabled = true
    target_port      = 8000

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }

    dynamic "ip_security_restriction" {
      for_each = data.cloudflare_ip_ranges.cloudflare.ipv4_cidrs
      content {
        name             = "cloudflare-${replace(ip_security_restriction.value, "/", "-")}"
        ip_address_range = ip_security_restriction.value
        action           = "Allow"
      }
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = 1

    container {
      name   = "backend"
      image  = local.backend_image
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "AZURE_TOKEN_CREDENTIALS"
        value = "managedidentitycredential"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.main.client_id
      }
      env {
        name        = "ORIGIN_HEADER_SECRET"
        secret_name = "origin-header-secret"
      }

      startup_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
    }

    container {
      name   = "cerbos"
      image  = local.cerbos_image
      cpu    = 0.25
      memory = "0.5Gi"
      args = [
        "server",
        "--set=storage.disk.directory=/policies",
        "--set=engine.strictEvaluation=true",
        "--set=schema.enforcement=reject",
      ]

      liveness_probe {
        transport = "HTTP"
        port      = 3592
        path      = "/_cerbos/health"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 3592
        path      = "/_cerbos/health"
      }

      volume_mounts {
        name = "policies"
        path = "/policies"
      }
    }

    container {
      name   = "mcp"
      image  = local.backend_image
      cpu    = 0.125
      memory = "0.25Gi"
      args   = ["--port", "8001"]

      liveness_probe {
        transport = "HTTP"
        port      = 8001
        path      = "/healthz"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8001
        path      = "/healthz"
      }
    }

    container {
      name   = "mockbank"
      image  = local.backend_image
      cpu    = 0.125
      memory = "0.25Gi"
      args   = ["--port", "8002"]

      liveness_probe {
        transport = "HTTP"
        port      = 8002
        path      = "/healthz"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8002
        path      = "/healthz"
      }
    }

    volume {
      name         = "policies"
      storage_type = "EmptyDir"
    }
  }

  # The provider reads back an empty value for a Key Vault reference (vendor note on secret).
  lifecycle {
    ignore_changes = [secret]
  }

  depends_on = [azurerm_role_assignment.acr_pull, azurerm_role_assignment.secrets_user]
}

# azurerm 5.11.0 has no argument for identitySettings.
resource "azapi_update_resource" "identity_lifecycle" {
  type        = "Microsoft.App/containerApps@2025-07-01"
  resource_id = azurerm_container_app.dcw.id

  body = {
    properties = {
      configuration = {
        identitySettings = [
          { identity = azurerm_user_assigned_identity.platform.id, lifecycle = "None" },
          { identity = azurerm_user_assigned_identity.main.id, lifecycle = "Main" },
        ]
      }
    }
  }
}

resource "azurerm_container_app_environment_certificate" "origin" {
  count = var.origin_certificate_issued ? 1 : 0

  name                         = "origin-certificate"
  container_app_environment_id = azurerm_container_app_environment.dcw.id

  certificate_key_vault {
    identity            = azurerm_user_assigned_identity.platform.id
    key_vault_secret_id = azurerm_key_vault_certificate.origin.versionless_secret_id
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_container_app_custom_domain" "dcw" {
  count = var.origin_certificate_issued ? 1 : 0

  name                                     = local.hostname
  container_app_id                         = azurerm_container_app.dcw.id
  container_app_environment_certificate_id = azurerm_container_app_environment_certificate.origin[0].id
  certificate_binding_type                 = "SniEnabled"

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [cloudflare_dns_record.verification]
}

resource "azurerm_container_app_job" "smoke" {
  name                         = "caj-dcw-smoke-eus2"
  location                     = data.azurerm_resource_group.dcw.location
  resource_group_name          = data.azurerm_resource_group.dcw.name
  container_app_environment_id = azurerm_container_app_environment.dcw.id
  replica_timeout_in_seconds   = 900
  replica_retry_limit          = 0

  schedule_trigger_config {
    cron_expression          = "0 12 * * *"
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.platform.id]
  }

  registry {
    server   = data.azurerm_container_registry.dcw.login_server
    identity = azurerm_user_assigned_identity.platform.id
  }

  secret {
    name                = "origin-header-secret"
    identity            = azurerm_user_assigned_identity.platform.id
    key_vault_secret_id = azurerm_key_vault_secret.origin_header.versionless_id
  }

  template {
    container {
      name    = "smoke"
      image   = local.backend_image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "dispute_casework.topology_smoke"]

      env {
        name  = "PUBLIC_URL"
        value = "https://${local.hostname}"
      }
      env {
        name  = "INGRESS_URL"
        value = "https://${azurerm_container_app.dcw.ingress[0].fqdn}"
      }
      env {
        name  = "INTERNAL_URL"
        value = "http://${azurerm_container_app.dcw.name}"
      }
      env {
        name        = "ORIGIN_HEADER_SECRET"
        secret_name = "origin-header-secret"
      }
    }
  }

  depends_on = [azurerm_role_assignment.acr_pull, azurerm_role_assignment.secrets_user]
}

resource "azapi_update_resource" "job_identity_lifecycle" {
  type        = "Microsoft.App/jobs@2025-07-01"
  resource_id = azurerm_container_app_job.smoke.id

  body = {
    properties = {
      configuration = {
        identitySettings = [
          { identity = azurerm_user_assigned_identity.platform.id, lifecycle = "None" },
        ]
      }
    }
  }
}
