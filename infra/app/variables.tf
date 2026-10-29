variable "domain" {
  type        = string
  description = "Apex domain of the Cloudflare zone that fronts the application. Supplied through TF_VAR_domain."
}

variable "subdomain" {
  type        = string
  description = "Host label under the domain that the application is served from. Supplied through TF_VAR_subdomain."
}

variable "image_tag" {
  type        = string
  description = "Tag of the backend image in the registry, as the image build produces it. Supplied through TF_VAR_image_tag."
}

variable "min_replicas" {
  type        = number
  description = "Minimum replicas of the application: 0 scales to zero between demos and for the cold-start measurement, 1 keeps one warm replica."
  default     = 0
}

variable "developer_ip_addresses" {
  type        = set(string)
  description = "IPv4 addresses allowed through the ingress beside Cloudflare's ranges while a measurement runs directly against it. Supplied through TF_VAR_developer_ip_addresses; empty otherwise."
  default     = []
}

variable "origin_header_secret" {
  type        = string
  description = "Value Cloudflare adds in the origin request header and the origin verifies. Supplied through TF_VAR_origin_header_secret, never committed; stored in Key Vault and in the Cloudflare rule."
  sensitive   = true
}

variable "origin_certificate_issued" {
  type        = bool
  description = "True once the signed Origin CA certificate has been merged into the Key Vault certificate, which lets the environment import it and bind the custom domain. Changed in the tree, not in the shell."
  default     = false
}
