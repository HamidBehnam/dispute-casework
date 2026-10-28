output "app_fqdn" {
  value = azurerm_container_app.dcw.ingress[0].fqdn
}

output "environment_static_ip" {
  value = azurerm_container_app_environment.dcw.static_ip_address
}

output "origin_certificate" {
  description = "Signed Origin CA certificate to merge into the Key Vault certificate request."
  value       = one(cloudflare_origin_ca_certificate.origin[*].certificate)
}
