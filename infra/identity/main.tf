data "azurerm_key_vault" "dcw" {
  name                = "kv-dcw-eus2"
  resource_group_name = "rg-dcw-eus2"
}

data "azuread_application_published_app_ids" "well_known" {}

resource "azuread_service_principal" "msgraph" {
  client_id    = data.azuread_application_published_app_ids.well_known.result["MicrosoftGraph"]
  use_existing = true
}

locals {
  hostname = "${var.subdomain}.${var.domain}"

  app_roles = {
    preparer          = "Opens, runs, edits and submits cases; never approves a case they prepared."
    approver          = "Approves, returns or rejects cases with a reason code; never a case they prepared."
    restricted_viewer = "Reads cases and the audit trail; sees public regulation text only."
  }
}

resource "azuread_application" "mcp" {
  display_name            = "dcw-mcp"
  sign_in_audience        = "AzureADMyOrg"
  prevent_duplicate_names = true

  api {
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = uuidv5("url", "https://dispute-casework/dcw-mcp/scopes/access_as_user")
      value                      = "access_as_user"
      type                       = "Admin"
      admin_consent_display_name = "Call the MCP tools as the signed-in user"
      admin_consent_description  = "Lets the backend call the MCP tools on behalf of the signed-in user."
    }
  }

  dynamic "app_role" {
    for_each = local.app_roles
    content {
      id                   = uuidv5("url", "https://dispute-casework/dcw-mcp/app-roles/${app_role.key}")
      value                = app_role.key
      display_name         = app_role.key
      description          = app_role.value
      allowed_member_types = ["User"]
    }
  }

  lifecycle {
    ignore_changes = [identifier_uris]
  }
}

resource "azuread_application" "api" {
  display_name            = "dcw-api"
  sign_in_audience        = "AzureADMyOrg"
  prevent_duplicate_names = true

  api {
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = uuidv5("url", "https://dispute-casework/dcw-api/scopes/access_as_user")
      value                      = "access_as_user"
      type                       = "Admin"
      admin_consent_display_name = "Use the casework API as the signed-in user"
      admin_consent_description  = "Lets the reviewer screen call the casework API on behalf of the signed-in user."
    }
  }

  dynamic "app_role" {
    for_each = local.app_roles
    content {
      id                   = uuidv5("url", "https://dispute-casework/dcw-api/app-roles/${app_role.key}")
      value                = app_role.key
      display_name         = app_role.key
      description          = app_role.value
      allowed_member_types = ["User"]
    }
  }

  required_resource_access {
    resource_app_id = azuread_application.mcp.client_id

    resource_access {
      id   = azuread_application.mcp.oauth2_permission_scope_ids["access_as_user"]
      type = "Scope"
    }
  }

  lifecycle {
    ignore_changes = [identifier_uris]
  }
}

resource "azuread_application" "web" {
  display_name            = "dcw-web"
  sign_in_audience        = "AzureADMyOrg"
  prevent_duplicate_names = true

  # MSAL's redirect bridge page, served by the backend on the application's origin.
  single_page_application {
    redirect_uris = ["https://${local.hostname}/redirect", "http://localhost:8000/redirect"]
  }

  required_resource_access {
    resource_app_id = azuread_application.api.client_id

    resource_access {
      id   = azuread_application.api.oauth2_permission_scope_ids["access_as_user"]
      type = "Scope"
    }
  }

  required_resource_access {
    resource_app_id = azuread_service_principal.msgraph.client_id

    dynamic "resource_access" {
      for_each = ["openid", "profile", "offline_access"]
      content {
        id   = azuread_service_principal.msgraph.oauth2_permission_scope_ids[resource_access.value]
        type = "Scope"
      }
    }
  }
}

resource "azuread_application_identifier_uri" "mcp" {
  application_id = azuread_application.mcp.id
  identifier_uri = "api://${azuread_application.mcp.client_id}"
}

resource "azuread_application_identifier_uri" "api" {
  application_id = azuread_application.api.id
  identifier_uri = "api://${azuread_application.api.client_id}"
}

# Tokens for the API and the MCP server are issued only to users assigned one of their roles.
resource "azuread_service_principal" "mcp" {
  client_id                    = azuread_application.mcp.client_id
  app_role_assignment_required = true
}

resource "azuread_service_principal" "api" {
  client_id                    = azuread_application.api.client_id
  app_role_assignment_required = true
}

resource "azuread_service_principal" "web" {
  client_id = azuread_application.web.client_id
}

# Admin consent for every delegated permission, so no persona ever sees a consent prompt.
resource "azuread_service_principal_delegated_permission_grant" "web_graph" {
  service_principal_object_id          = azuread_service_principal.web.object_id
  resource_service_principal_object_id = azuread_service_principal.msgraph.object_id
  claim_values                         = ["openid", "profile", "offline_access"]
}

resource "azuread_service_principal_delegated_permission_grant" "web_api" {
  service_principal_object_id          = azuread_service_principal.web.object_id
  resource_service_principal_object_id = azuread_service_principal.api.object_id
  claim_values                         = ["access_as_user"]
}

resource "azuread_service_principal_delegated_permission_grant" "api_mcp" {
  service_principal_object_id          = azuread_service_principal.api.object_id
  resource_service_principal_object_id = azuread_service_principal.mcp.object_id
  claim_values                         = ["access_as_user"]
}

# The backend signs its On-Behalf-Of client assertion in process from the PKCS#12
# secret, so the key is exportable. There is no lifetime action: a renewed
# certificate is not trusted by Entra until this root uploads it, so renewal is an
# apply before the date in the obo_certificate_expires output.
resource "azurerm_key_vault_certificate" "obo" {
  name         = "obo-certificate"
  key_vault_id = data.azurerm_key_vault.dcw.id

  certificate_policy {
    issuer_parameters {
      name = "Self"
    }

    key_properties {
      exportable = true
      key_size   = 2048
      key_type   = "RSA"
      reuse_key  = false
    }

    secret_properties {
      content_type = "application/x-pkcs12"
    }

    x509_certificate_properties {
      subject            = "CN=dcw-api"
      key_usage          = ["digitalSignature"]
      extended_key_usage = ["1.3.6.1.5.5.7.3.2"]
      validity_in_months = 12
    }
  }
}

resource "azuread_application_certificate" "obo" {
  application_id = azuread_application.api.id
  type           = "AsymmetricX509Cert"
  encoding       = "hex"
  value          = azurerm_key_vault_certificate.obo.certificate_data
  start_date     = azurerm_key_vault_certificate.obo.certificate_attribute[0].not_before
  end_date       = azurerm_key_vault_certificate.obo.certificate_attribute[0].expires
}
