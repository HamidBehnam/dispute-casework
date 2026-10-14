variable "entra_administrator_name" {
  type        = string
  description = "User principal name of the signed-in principal, which becomes the server's Entra administrator. Supplied through TF_VAR_entra_administrator_name."
}

variable "developer_ip_addresses" {
  type        = set(string)
  description = "IPv4 addresses allowed through the server firewall for development. Supplied through TF_VAR_developer_ip_addresses; with none, the server accepts no connection."
  default     = []
}
