data "azurerm_client_config" "current" {}

data "azuread_client_config" "current" {}

data "azurerm_resource_group" "dcw" {
  name = "rg-dcw-eus2"
}

resource "azuread_group" "postgres_administrators" {
  display_name            = "psql-dcw-eus2-administrators"
  security_enabled        = true
  prevent_duplicate_names = true
  owners                  = [data.azuread_client_config.current.object_id]
  members                 = [data.azuread_client_config.current.object_id]
}

resource "azuread_user" "test" {
  user_principal_name = var.test_user_principal_name
  display_name        = var.test_user_display_name
  password            = var.test_user_password
}

resource "azurerm_postgresql_flexible_server" "dcw" {
  name                          = "psql-dcw-eus2"
  resource_group_name           = data.azurerm_resource_group.dcw.name
  location                      = data.azurerm_resource_group.dcw.location
  version                       = "17"
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  public_network_access_enabled = true

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "administrators" {
  server_name         = azurerm_postgresql_flexible_server.dcw.name
  resource_group_name = data.azurerm_resource_group.dcw.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = azuread_group.postgres_administrators.object_id
  principal_name      = azuread_group.postgres_administrators.display_name
  principal_type      = "Group"
}

resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.dcw.id
  value     = "vector"
}

# Created only after the Entra administrator exists on the server.
resource "azurerm_postgresql_flexible_server_database" "dcw" {
  name      = "dcw"
  server_id = azurerm_postgresql_flexible_server.dcw.id

  depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.administrators]

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "developer" {
  for_each = var.developer_ip_addresses

  name             = "developer-${replace(each.value, ".", "-")}"
  server_id        = azurerm_postgresql_flexible_server.dcw.id
  start_ip_address = each.value
  end_ip_address   = each.value
}
