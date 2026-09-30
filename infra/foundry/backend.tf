terraform {
  backend "azurerm" {
    resource_group_name  = "rg-dcw-eus2"
    storage_account_name = "stdcwtfstate"
    container_name       = "tfstate"
    key                  = "foundry.tfstate"
    use_azuread_auth     = true
  }
}
