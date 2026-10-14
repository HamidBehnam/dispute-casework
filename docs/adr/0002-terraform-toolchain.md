# ADR 0002: Terraform toolchain

Status: Accepted

## Context

Azure resources for this service are provisioned as reviewed, idempotent code with a plan step, never by hand. The first stack is a Foundry account with five model deployments, a subscription budget and a quota-tier setting that the azurerm provider does not expose. State must live outside the developer machine from the first apply, and CI must validate every root without cloud credentials.

## Decision

- Terraform 1.16.4, selected by `.terraform-version` through tenv and pinned again by `required_version` in every root.
- Providers: azurerm 5.7.0 and azapi 2.13.0, pinned exactly, with `.terraform.lock.hcl` committed per root for `linux_amd64` and `darwin_arm64`.
- azurerm runs with `resource_provider_registrations = "none"`; the providers this repository uses (Microsoft.CognitiveServices, Microsoft.Storage, Microsoft.Consumption) were already registered on the subscription, so no registration resource is declared.
- Subscription and tenant come only from `ARM_SUBSCRIPTION_ID` and `ARM_TENANT_ID`. The backend authenticates with the signed-in Azure CLI identity over Entra (`ARM_USE_AZUREAD`, `ARM_USE_CLI`, `use_azuread_auth`), and the provider sets `storage_use_azuread = true`, so no storage key is ever read.
- Two roots. `infra/bootstrap` creates the resource group `rg-dcw-eus2` and the state account `stdcwtfstate` (Standard LRS, shared-key access off, TLS 1.2, blob versioning and 30-day soft delete, container `tfstate`, Storage Blob Data Contributor to the signed-in principal); it was applied with local state and then migrated into its own container under the key `bootstrap.tfstate`. `infra/foundry` holds the workload and stores its state under `foundry.tfstate`.
- Naming: workload token `dcw`, region suffix `eus2`, no environment segment, because there is one environment.
- CI runs `terraform fmt -check -recursive`, `init -backend=false` and `validate` per root with `hashicorp/setup-terraform` pinned by commit SHA; Dependabot tracks the providers per root.
- `.gitignore` excludes `.terraform/`, state files, `*.tfvars`, lock info and crash logs. Values that must not be committed, such as the budget contact addresses, arrive as `TF_VAR_` environment variables.

## Rationale

- Terraform over Bicep: one language for Azure and the Cloudflare edge that arrives later, and the azapi provider covers preview API surfaces without leaving the plan and apply workflow.
- tenv over a bare binary: the version file makes the pin visible in the repository, and `required_version` rejects any other binary, locally and in CI.
- A separate bootstrap root: the state backend cannot store its own creation; the bootstrap-then-migrate sequence is the standard way to get there with no manually created resource.
- Entra-only storage access over a shared key: nothing to rotate, and the role assignment is itself in the plan.

## Consequences

- Every provider or Terraform release is a Dependabot pull request that touches the lock files.
- A new machine needs tenv, the Azure CLI signed in as a principal with Storage Blob Data Contributor on the state account, and the two `ARM_` identifiers exported.
- Registering an additional resource provider is a deliberate change to the bootstrap root, not a side effect of an apply.
