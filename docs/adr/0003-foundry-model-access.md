# ADR 0003: Foundry model access

Status: Accepted

## Context

Every model call in this service goes through one Microsoft Foundry account. The spike had to prove that the five deployments the design needs exist in East US 2 with automatic upgrades off, that each answers one call through the LangChain integration, that the reranker route accepts an Entra token, and that spend is capped before any application code exists.

## Decision

- One `AIServices` account, `ai-dcw-eus2`, SKU S0, custom subdomain `ai-dcw-eus2`, project management enabled, system-assigned identity. Local (key) authentication stays enabled for the spike; the application path is Entra.
- The signed-in principal holds `Foundry User` at the account scope. It grants the data actions for inference without the management rights of the account-owner roles.
- Five deployments named exactly after their models, GlobalStandard, `NoAutoUpgrade`: gpt-5.4-mini 2026-03-17, gpt-5.4-nano 2026-03-17, text-embedding-3-large 1, DeepSeek-V4-Pro 2026-04-23, Cohere-rerank-v4.0-fast 1. Capacities are in `MODELS.md`.
- The subscription quota tier is set to `tierUpgradePolicy = NoAutoUpgrade` through `azapi_update_resource` on `Microsoft.CognitiveServices/quotaTiers/default` at api-version 2025-10-01-preview, because azurerm has no resource for it.
- A subscription budget of 25 USD per month with alerts at 50, 80 and 100 percent actual and 100 percent forecast. The contact address arrives through `TF_VAR_budget_email`.
- Python: langchain-azure-ai 1.2.10, langchain 1.4.3, azure-identity 1.25.3 and cohere 7.2.0, the current release on PyPI on 2026-09-30. `foundry.py` reads `FOUNDRY_ENDPOINT` and an optional `FOUNDRY_KEY`; the credential is the key when set, otherwise `AzureCliCredential`. Chat models are `AzureAIOpenAIApiChatModel` on `<endpoint>/openai/v1`, on the Responses API for OpenAI models and on chat completions for DeepSeek. Embeddings are `AzureAIOpenAIApiEmbeddingsModel` at 1536 dimensions. The reranker is the cohere `ClientV2` at `<endpoint>/providers/cohere` with a bearer-token callable.

## Findings

Readback on 2026-09-30 (`infra/foundry/readback.sh`):

- Account: kind AIServices, S0, endpoint `https://ai-dcw-eus2.cognitiveservices.azure.com/`, `allowProjectManagement = true`, `disableLocalAuth = false`.
- Deployments created: gpt-5.4-mini, gpt-5.4-nano, text-embedding-3-large at capacity 10; DeepSeek-V4-Pro at capacity 1. All `NoAutoUpgrade`, all `Succeeded`.
- Capacity unit: `rateLimits` at capacity 10 read `token 10000 per 60 s` and `request 10 per 60 s`; at capacity 1 for DeepSeek-V4-Pro, `token 1000` and `request 1`. One unit is 1K tokens per minute and 1 request per minute for chat models; the embedding deployment reads `request 10 per 10 s`.
- Cohere-rerank-v4.0-fast was refused at capacity 1 on two attempts with HTTP 400 `715-123420: Our system has detected this request as unusual activity for your account. If you are confident this is in error, please contact support.` Raising DeepSeek-V4-Pro from 1 to 5 was refused with the same code. The deployment stays declared at capacity 1 and DeepSeek-V4-Pro stays at 1, the value the API accepted, until Azure support lifts the block; `terraform plan` shows the one pending create.
- Quota tier: the account started at `currentTierName = Tier 1`, `tierUpgradePolicy = OnceUpgradeIsAvailable`. The azapi PUT at 2025-10-01-preview returned 200 echoing `NoAutoUpgrade`, but reads at 2025-10-01-preview, 2026-01-15-preview and 2026-09-01 kept returning `OnceUpgradeIsAvailable` for about twenty minutes, during which a PATCH at 2025-10-01-preview and a PUT at 2026-01-15-preview were also acknowledged. The read then converged to `NoAutoUpgrade`, and the plan is clean. The write is eventually consistent; a read directly after an apply is not a verification.
- Smoke calls (`dispute_casework.foundry_smoke`), Entra token via `AzureCliCredential` and then the account key, both against the account endpoint: gpt-5.4-mini, gpt-5.4-nano and DeepSeek-V4-Pro on chat completions answered with both credentials; text-embedding-3-large returned 1536 dimensions with both. DeepSeek-V4-Pro on the Responses path returned HTTP 429 `code: no_capacity` on three attempts, including one made alone after a minute of idle time, while chat completions succeeded seconds earlier; DeepSeek stays on chat completions.
- Rerank: both the Entra token and the key reached the Cohere route and were answered with HTTP 404 `DeploymentNotFound` for `cohere-rerank-v4.0-fast`, not 401, so both credentials pass the gateway. Whether `max_tokens_per_doc` is accepted is undetermined until the deployment exists; the smoke script retries without it on a 400 that names the parameter.
- Cohere quota: `az cognitiveservices usage list -l eastus2` shows `AIServices.GlobalStandard.Cohere-Rerank-V4-Fast` with a limit of 20 units, an observed value rather than a published limit. The design's retrieval rate needs more; a quota increase to 100K tokens per minute is requested through the Microsoft quota form.

## Rationale

- One Foundry account over a classic Azure OpenAI resource plus separate serverless endpoints: one endpoint, one identity, one quota view, and non-OpenAI models deployed as first-class deployments.
- `NoAutoUpgrade` on every deployment and on the quota tier: the eval baselines are only comparable while the model version and the rate limits stay where they were measured.
- Entra first, key second: the application authenticates as a managed identity; the key exists only so the spike can tell an authorization failure from a deployment failure.
- langchain-azure-ai over langchain-openai directly: one credential type across chat, embeddings and, later, tools, and the project-endpoint path when the agent service is added.
- The cohere SDK over a hand-written HTTP call: the Foundry route speaks the Cohere v2 API, and the SDK owns the request shape.

## Consequences

- `infra/foundry` will plan one create until the Cohere deployment is accepted; the DeepSeek capacity target of 5 is re-applied by editing the map once the block is lifted.
- Local authentication is turned off in a later step once the application runs under a managed identity; that change supersedes the corresponding line here.
- Any deployment version change is a deliberate edit to the deployment map and a new eval baseline.
