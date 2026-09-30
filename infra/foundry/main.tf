data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "dcw" {
  name = "rg-dcw-eus2"
}

resource "azurerm_cognitive_account" "foundry" {
  name                       = "ai-dcw-eus2"
  resource_group_name        = data.azurerm_resource_group.dcw.name
  location                   = data.azurerm_resource_group.dcw.location
  kind                       = "AIServices"
  sku_name                   = "S0"
  custom_subdomain_name      = "ai-dcw-eus2"
  project_management_enabled = true
  local_auth_enabled         = true

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "foundry_user" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Foundry User"
  principal_id         = data.azurerm_client_config.current.object_id
}

locals {
  deployments = {
    "gpt-5.4-mini"            = { format = "OpenAI", version = "2026-03-17", capacity = 10 }
    "gpt-5.4-nano"            = { format = "OpenAI", version = "2026-03-17", capacity = 10 }
    "text-embedding-3-large"  = { format = "OpenAI", version = "1", capacity = 10 }
    "DeepSeek-V4-Pro"         = { format = "DeepSeek", version = "2026-04-23", capacity = 1 }
    "Cohere-rerank-v4.0-fast" = { format = "Cohere", version = "1", capacity = 1 }
  }
}

resource "azurerm_cognitive_deployment" "model" {
  for_each = local.deployments

  name                   = each.key
  cognitive_account_id   = azurerm_cognitive_account.foundry.id
  version_upgrade_option = "NoAutoUpgrade"

  model {
    format  = each.value.format
    name    = each.key
    version = each.value.version
  }

  sku {
    name     = "GlobalStandard"
    capacity = each.value.capacity
  }
}

resource "azapi_update_resource" "quota_tier" {
  type        = "Microsoft.CognitiveServices/quotaTiers@2025-10-01-preview"
  resource_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/providers/Microsoft.CognitiveServices/quotaTiers/default"

  body = {
    properties = {
      tierUpgradePolicy = "NoAutoUpgrade"
    }
  }
}

resource "azurerm_consumption_budget_subscription" "dcw" {
  name            = "budget-dcw"
  subscription_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}"
  amount          = 25
  time_grain      = "Monthly"

  time_period {
    start_date = "2026-09-01T00:00:00Z"
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      enabled        = true
      threshold      = notification.value
      operator       = "GreaterThanOrEqualTo"
      threshold_type = "Actual"
      contact_emails = [var.budget_email]
    }
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Forecasted"
    contact_emails = [var.budget_email]
  }
}
