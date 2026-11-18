output "api_client_id" {
  value = azuread_application.api.client_id
}

output "mcp_client_id" {
  value = azuread_application.mcp.client_id
}

output "web_client_id" {
  value = azuread_application.web.client_id
}

output "obo_certificate_secret_id" {
  description = "Versionless Key Vault secret holding the On-Behalf-Of certificate and its key (PKCS#12)."
  value       = azurerm_key_vault_certificate.obo.versionless_secret_id
}

output "obo_certificate_expires" {
  value = azurerm_key_vault_certificate.obo.certificate_attribute[0].expires
}
