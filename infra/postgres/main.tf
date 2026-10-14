data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "dcw" {
  name = "rg-dcw-eus2"
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
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "signed_in" {
  server_name         = azurerm_postgresql_flexible_server.dcw.name
  resource_group_name = data.azurerm_resource_group.dcw.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = data.azurerm_client_config.current.object_id
  principal_name      = var.entra_administrator_name
  principal_type      = "User"
}

resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.dcw.id
  value     = "VECTOR"
}

# Created after Entra authentication is on, so that an Entra principal can own it.
resource "azurerm_postgresql_flexible_server_database" "dcw" {
  name      = "dcw"
  server_id = azurerm_postgresql_flexible_server.dcw.id

  depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.signed_in]
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "developer" {
  for_each = var.developer_ip_addresses

  name             = "developer-${replace(each.value, ".", "-")}"
  server_id        = azurerm_postgresql_flexible_server.dcw.id
  start_ip_address = each.value
  end_ip_address   = each.value
}
