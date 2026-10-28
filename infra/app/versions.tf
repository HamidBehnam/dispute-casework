terraform {
  required_version = "1.16.4"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "5.11.0"
    }
    azapi = {
      source  = "azure/azapi"
      version = "2.13.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.28.1"
    }
  }
}

provider "azurerm" {
  resource_provider_registrations = "none"
  storage_use_azuread             = true
  features {}
}

provider "azapi" {}

# Authenticates with CLOUDFLARE_API_TOKEN from the environment.
provider "cloudflare" {}
