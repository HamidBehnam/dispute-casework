# ADR 0003: Foundry model access

Status: Accepted. Superseded by 0006 in part.

## Context

Every model call in this service goes through one Microsoft Foundry account. The spike had to prove that the five deployments the design needs exist in East US 2 with automatic upgrades off, that each answers one call through the LangChain integration, that the reranker route accepts an Entra token, and that spend is capped before any application code exists.

## Decision

- One `AIServices` account, `ai-dcw-eus2`, SKU S0, custom subdomain `ai-dcw-eus2`, project management enabled, system-assigned identity. Local (key) authentication stays enabled for the spike; the application path is Entra.
- The signed-in principal holds `Foundry User` at the account scope. It grants the data actions for inference without the management rights of the account-owner roles.
- Five deployments named exactly after their models, GlobalStandard, `NoAutoUpgrade`: gpt-5.4-mini 2026-03-17, gpt-5.4-nano 2026-03-17, text-embedding-3-large 1, DeepSeek-V4-Pro 2026-04-23, Cohere-rerank-v4.0-fast 1. Capacities are in `MODELS.md`.
- The subscription quota tier is set to `tierUpgradePolicy = NoAutoUpgrade` through `azapi_update_resource` on `Microsoft.CognitiveServices/quotaTiers/default` at api-version 2025-10-01-preview, because azurerm has no resource for it.
- A subscription budget of 25 USD per month with alerts at 50, 80 and 100 percent actual and 100 percent forecast. The contact addresses arrive through `TF_VAR_budget_emails`.
- Python: the direct dependencies for model access are langchain-azure-ai 1.2.10, azure-identity 1.25.3, cohere 7.2.0, pydantic 2.13.5 and pydantic-settings 2.15.0, at the versions in `uv.lock`. `foundry.py` reads `FOUNDRY_ENDPOINT` and an optional `FOUNDRY_KEY` from the environment through a pydantic-settings class: the key is a `SecretStr`, an empty key counts as none and the endpoint is kept without a trailing slash. Chat and embedding clients are built with `api_key=` and never `credential=`: the key string when one is set, otherwise `get_bearer_token_provider(AzureCliCredential(), "https://ai.azure.com/.default")`. Both carry an explicit timeout of 60 seconds and no `api_version`. Chat models are `AzureAIOpenAIApiChatModel` on `<endpoint>/openai/v1`, on the Responses API for OpenAI models and on chat completions for DeepSeek. Embeddings are `AzureAIOpenAIApiEmbeddingsModel` at 1536 dimensions with 16 inputs a request. The reranker is the cohere `ClientV2` at `<endpoint>/providers/cohere` with the key, or with a bearer-token provider on `https://cognitiveservices.azure.com/.default`, with the same timeout and `max_retries=7`; `MODELS.md` gives the reason for the retries.

## Findings

- Account (readback through `infra/foundry/readback.sh`, 2026-10-01): kind AIServices, S0, endpoint `https://ai-dcw-eus2.cognitiveservices.azure.com/`, `allowProjectManagement = true`, `disableLocalAuth = false`.
- Deployments (readback, 2026-10-01): all five exist, GlobalStandard and `NoAutoUpgrade`: gpt-5.4-mini, gpt-5.4-nano and text-embedding-3-large at capacity 10, DeepSeek-V4-Pro at 5, Cohere-rerank-v4.0-fast at 20. `terraform plan` shows no changes.
- Capacity unit (readback, 2026-10-01): `rateLimits` read `token 10000 per 60 s` and `request 10 per 60 s` at capacity 10, `token 5000` and `request 5` at capacity 5, and `token 20000` and `request 20` at capacity 20; `MODELS.md` states the unit and the embedding deployment's request limit.
- Capacity changes (Activity Log of the resource group, read 2026-10-06): on 2026-09-30 the first two creates of Cohere-rerank-v4.0-fast and three later writes of DeepSeek-V4-Pro were refused with HTTP 400 `715-123420: Our system has detected this request as unusual activity for your account. If you are confident this is in error, please contact support.`; the third create of Cohere-rerank-v4.0-fast was accepted and no update of it was refused. The application ID in each event's claims separates two client applications: ten writes, the creates and the three refused writes of DeepSeek-V4-Pro, carry the application ID of the Azure CLI's tokens, the login Terraform authenticates with in this project (ADR 0002), and the log has no field that tells Terraform from a direct CLI command. Two writes carry another application ID, one accepted update of each of the two deployments: the raises made in the Foundry portal, DeepSeek-V4-Pro from 1 to 5 and Cohere-rerank-v4.0-fast from 1 to 20, mirrored in the deployment map. The log does not record the capacity a write asked for. Support found no restriction, flag or block on the account and gave no cause for the refusals of 2026-09-30; investigating them would need a technical support request under a Standard or higher support plan. On 2026-10-05 two writes of DeepSeek-V4-Pro were accepted, both carrying the application ID of the Azure CLI's tokens, and none refused.
- Quota tier (readback, 2026-10-01): the azapi write of `tierUpgradePolicy = NoAutoUpgrade` is eventually consistent. The PUT returned 200 echoing the value while reads kept returning `OnceUpgradeIsAvailable` for about twenty minutes before converging; a read directly after an apply is not a verification.
- Client construction (langchain-azure-ai 1.2.10 source): given `credential=` with a token credential, the model class builds the OpenAI SDK client itself and passes neither `timeout` nor `max_retries` to it, so the values set on the model are not applied; given a key string and no explicit timeout, the SDK client has none. Given `api_key=` as a key string or as a token-provider callable, the model builds the SDK client with the configured timeout and retry count, on the same URL and with the same request body. A test builds both forms offline and reads the timeout from the SDK client (`INVARIANTS.md`).
- Smoke calls (2026-10-06, `dispute_casework.foundry_smoke`): with the chat and embedding clients built as above, all five deployments answered under the Entra token of the Azure CLI login and under the account key, both against the account endpoint. gpt-5.4-mini and gpt-5.4-nano answered on the Responses API and DeepSeek-V4-Pro on chat completions; text-embedding-3-large returned 1536 dimensions; Cohere-rerank-v4.0-fast returned a ranking.
- DeepSeek-V4-Pro on the Responses path (first recorded 2026-09-30), tried at capacity 1, returned HTTP 429 `code: no_capacity` on three attempts, including one made alone after a minute of idle time, while chat completions succeeded seconds earlier; DeepSeek stays on chat completions.
- Cohere quota (first recorded 2026-09-30): `az cognitiveservices usage list -l eastus2` shows `AIServices.GlobalStandard.Cohere-Rerank-V4-Fast` with a limit of 20 units, an observed value rather than a published limit, and the deployment uses all of it. The design's retrieval rate needs more; no quota increase has been requested (first recorded 2026-10-06).
- Budget (first recorded 2026-10-01): the notifications carry two contact addresses, one of them added by Azure. The configuration lists both, so the plan stays clean.

## Rationale

- One Foundry account over a classic Azure OpenAI resource plus separate serverless endpoints: one endpoint, one identity, one quota view, and non-OpenAI models deployed as first-class deployments.
- `NoAutoUpgrade` on every deployment and on the quota tier: the eval baselines are only comparable while the model version and the rate limits stay where they were measured.
- Entra first, key second: the application authenticates as a managed identity; the key exists only so the spike can tell an authorization failure from a deployment failure.
- langchain-azure-ai over langchain-openai directly: one credential type across chat, embeddings and, later, tools, and the project-endpoint path when the agent service is added.
- The cohere SDK over a hand-written HTTP call: the Foundry route speaks the Cohere v2 API, and the SDK owns the request shape.

## Consequences

- Capacity changes go through Terraform. If error 715-123420 recurs, the change is made in the Foundry portal and mirrored in the deployment map in `infra/foundry/main.tf`, so that `terraform plan` shows no changes.
- The Cohere deployment cannot grow past capacity 20 unless a quota increase is requested and granted.
- Local authentication is turned off in a later step once the application runs under a managed identity; that change is recorded in a new ADR.
- Any deployment version change is a deliberate edit to the deployment map and a new eval baseline.
