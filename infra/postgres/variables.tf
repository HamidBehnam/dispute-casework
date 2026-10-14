variable "developer_ip_addresses" {
  type        = set(string)
  description = "IPv4 addresses allowed through the server firewall for development. Supplied through TF_VAR_developer_ip_addresses; with none, the server accepts no connection."
  default     = []
}

variable "test_user_principal_name" {
  type        = string
  description = "User principal name of the Entra test user the application signs in as during the spike. Supplied through TF_VAR_test_user_principal_name."
}

variable "test_user_display_name" {
  type        = string
  description = "Display name of the Entra test user. Supplied through TF_VAR_test_user_display_name."
}

variable "test_user_password" {
  type        = string
  description = "Initial password of the Entra test user. Supplied through TF_VAR_test_user_password, never committed and never printed; Terraform stores it in the remote state, so the test user is rotated or deleted after the spike."
  sensitive   = true
}
