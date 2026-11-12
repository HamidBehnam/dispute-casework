variable "identity_tenant_id" {
  type        = string
  description = "Tenant that holds the application registrations and the personas. Supplied through TF_VAR_identity_tenant_id."
}

variable "domain" {
  type        = string
  description = "Apex domain of the Cloudflare zone that fronts the application. Supplied through TF_VAR_domain."
}

variable "subdomain" {
  type        = string
  description = "Host label under the domain that the application is served from. Supplied through TF_VAR_subdomain."
}
