variable "developer_ip_addresses" {
  type        = set(string)
  description = "IPv4 addresses allowed through the server firewall for development. Supplied through TF_VAR_developer_ip_addresses; with none, the server accepts no connection."
  default     = []
}
