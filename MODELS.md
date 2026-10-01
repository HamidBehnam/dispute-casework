# Models

Deployments on the Foundry account `ai-dcw-eus2` (East US 2), declared in `infra/foundry/main.tf`. All are GlobalStandard with `versionUpgradeOption = NoAutoUpgrade`. One capacity unit is 1,000 tokens per minute and 1 request per minute, as read back from `properties.rateLimits`; the embedding deployment's request limit reads 10 per 10 seconds. The Foundry portal shows "N/A" in its capacity column for the non-OpenAI models, while the API reports their capacity.

| Deployment | Version | SKU | Capacity | Retirement | Purpose |
|---|---|---|---|---|---|
| gpt-5.4-mini | 2026-03-17 | GlobalStandard | 10 | 2027-09-21 | agent: triage, drafting, tool calling |
| gpt-5.4-nano | 2026-03-17 | GlobalStandard | 10 | 2027-09-21 | fallback routing and cheap classification |
| text-embedding-3-large | 1 | GlobalStandard | 10 | 2028-02-09 | corpus and query embeddings at 1536 dimensions |
| DeepSeek-V4-Pro | 2026-04-23 | GlobalStandard | 5 | 2028-02-20 | evaluation judge from a second model family |
| Cohere-rerank-v4.0-fast | 1 | GlobalStandard | 20 | none announced | rerank of retrieval candidates |

Embedding requests carry raw strings (`check_embedding_ctx_length=False`): no tokenizer runs in the embedding path, and an input over the model's 8,191-token limit fails at the API instead of being split and averaged.

Retirement dates are `model.deprecation.inference` from `az cognitiveservices model list -l eastus2` on 2026-09-30. The subscription-wide quota for DeepSeek-V4-Pro and Cohere-rerank-v4.0-fast in East US 2 was observed as 20 units each in `az cognitiveservices usage list`; this is an observation, not a published limit. Until the support request in ADR 0003 clears, capacities are changed in the Foundry portal and mirrored in `infra/foundry/main.tf`.
