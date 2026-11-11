variable "domain" {
  type        = string
  description = "Apex domain of the Cloudflare zone that fronts the application. Supplied through TF_VAR_domain."
}

variable "subdomain" {
  type        = string
  description = "Host label under the domain that the application is served from. Supplied through TF_VAR_subdomain."
}

variable "image_digest" {
  type        = string
  description = "Digest of the backend image in the registry (sha256:<64 hex>), as the image push reports it. Supplied through TF_VAR_image_digest."

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "The backend image is referenced by digest: sha256: followed by 64 lowercase hex characters."
  }
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
  description = "Value Cloudflare adds in the origin request header and the origin verifies. Supplied through TF_VAR_origin_header_secret, never committed; stored in Key Vault and in the Cloudflare rule, which holds it as a plain value, so it is also in this root's Terraform state and in any saved plan."
  sensitive   = true

  validation {
    condition     = length(var.origin_header_secret) >= 32
    error_message = "The origin header secret must be at least 32 characters; the origin refuses to start on a shorter one."
  }
}

variable "origin_certificate_issued" {
  type        = bool
  description = "True once the signed Origin CA certificate has been merged into the Key Vault certificate, which lets the environment import it and bind the custom domain. Changed in the tree, not in the shell."
  default     = true
}
