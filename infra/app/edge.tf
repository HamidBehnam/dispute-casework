data "cloudflare_zone" "dcw" {
  filter = {
    name = var.domain
  }
}

data "cloudflare_ip_ranges" "cloudflare" {}

resource "cloudflare_zone_setting" "ssl" {
  zone_id    = data.cloudflare_zone.dcw.id
  setting_id = "ssl"
  value      = "strict"
}

resource "cloudflare_dns_record" "app" {
  zone_id = data.cloudflare_zone.dcw.id
  name    = var.subdomain
  type    = "A"
  content = azurerm_container_app_environment.dcw.static_ip_address
  proxied = true
  ttl     = 1
}

resource "cloudflare_dns_record" "verification" {
  zone_id = data.cloudflare_zone.dcw.id
  name    = "asuid.${var.subdomain}"
  type    = "TXT"
  content = azurerm_container_app.dcw.custom_domain_verification_id
  proxied = false
  ttl     = 1
}

resource "cloudflare_origin_ca_certificate" "origin" {
  count = var.origin_csr == "" ? 0 : 1

  csr                = var.origin_csr
  hostnames          = [local.hostname]
  request_type       = "origin-rsa"
  requested_validity = 5475

  # Destroying revokes the certificate; a changed CSR would revoke and reissue.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [csr]
  }
}

resource "cloudflare_ruleset" "origin_header" {
  zone_id = data.cloudflare_zone.dcw.id
  name    = "Origin request header"
  kind    = "zone"
  phase   = "http_request_late_transform"

  rules = [{
    description = "Mark requests that passed through Cloudflare"
    expression  = "(http.host eq \"${local.hostname}\")"
    action      = "rewrite"
    action_parameters = {
      headers = {
        "X-Origin-Secret" = {
          operation = "set"
          value     = var.origin_header_secret
        }
      }
    }
  }]
}

resource "cloudflare_ruleset" "rate_limit" {
  zone_id = data.cloudflare_zone.dcw.id
  name    = "Rate limit"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules = [{
    description = "Block a client that sends more than 100 requests in 10 seconds"
    expression  = "(starts_with(http.request.uri.path, \"/\"))"
    action      = "block"
    ratelimit = {
      characteristics     = ["cf.colo.id", "ip.src"]
      period              = 10
      requests_per_period = 100
      mitigation_timeout  = 10
    }
  }]
}
