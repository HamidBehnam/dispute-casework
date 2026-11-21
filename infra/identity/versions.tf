terraform {
  required_version = "1.16.4"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "5.14.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "3.10.0"
    }
  }
}

provider "azurerm" {
  resource_provider_registrations = "none"
  storage_use_azuread             = true
  features {}
}

# The tenant that holds the registrations and the personas is named explicitly,
# so the provider never falls back to the subscription's tenant by accident.
provider "azuread" {
  tenant_id = var.identity_tenant_id
}
