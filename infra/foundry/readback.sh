#!/usr/bin/env bash
# Prints what the Foundry stack looks like from the API, for the ADR and MODELS.md.
set -euo pipefail

rg=rg-dcw-eus2
account=ai-dcw-eus2
subscription=$(az account show --query id -o tsv)

echo "== deployments"
az cognitiveservices account deployment list -g "$rg" -n "$account" \
  --query "[].{name:name, model:properties.model.name, version:properties.model.version, format:properties.model.format, sku:sku.name, capacity:sku.capacity, rateLimits:properties.rateLimits[].{key:key, count:count, renewalPeriod:renewalPeriod}, versionUpgradeOption:properties.versionUpgradeOption}" \
  -o json

echo "== account"
az cognitiveservices account show -g "$rg" -n "$account" \
  --query "{kind:kind, sku:sku.name, endpoint:properties.endpoint, allowProjectManagement:properties.allowProjectManagement, disableLocalAuth:properties.disableLocalAuth}" \
  -o json

echo "== quota tier"
az rest --method get \
  --url "/subscriptions/$subscription/providers/Microsoft.CognitiveServices/quotaTiers/default?api-version=2025-10-01-preview" \
  -o json
