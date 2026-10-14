output "fqdn" {
  value = azurerm_postgresql_flexible_server.dcw.fqdn
}

output "database" {
  value = azurerm_postgresql_flexible_server_database.dcw.name
}
