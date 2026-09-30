data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "dcw" {
  name     = "rg-dcw-eus2"
  location = "eastus2"
}

resource "azurerm_storage_account" "tfstate" {
  name                            = "stdcwtfstate"
  resource_group_name             = azurerm_resource_group.dcw.name
  location                        = azurerm_resource_group.dcw.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = 30
    }

    container_delete_retention_policy {
      days = 30
    }
  }
}

resource "azurerm_storage_container" "tfstate" {
  name               = "tfstate"
  storage_account_id = azurerm_storage_account.tfstate.id
}

resource "azurerm_role_assignment" "tfstate_writer" {
  scope                = azurerm_storage_account.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}
