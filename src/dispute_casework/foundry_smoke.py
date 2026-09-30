"""One call per deployment against the Foundry account, printing what each returned.

Run with FOUNDRY_ENDPOINT set; FOUNDRY_KEY is optional and is tried for the
reranker only after the Entra token. Results are recorded in ADR 0003.
"""

import sys
from collections.abc import Callable

from azure.identity import AzureCliCredential, get_bearer_token_provider
from cohere import ClientV2
from cohere.core import ApiError

from dispute_casework.foundry import (
    EMBEDDING_DIMENSIONS,
    TOKEN_SCOPE,
    FoundrySettings,
    chat_model,
    cohere_base_url,
    embeddings,
)

PROMPT = "Reply with the single word: ready"
QUERY = "When must a bank provisionally credit a consumer's account?"
DOCUMENTS = [
    "Regulation E requires provisional credit within 10 business days when the "
    "investigation takes longer.",
    "Regulation Z covers billing error disputes on open-end credit accounts.",
    "The bank calendar treats a Saturday holiday as a business day.",
]


def check_dimensions(vector: list[float]) -> None:
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"expected {EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}"
        )


def record(label: str, call: Callable[[], str]) -> bool:
    try:
        print(f"{label}: ok - {call()}")
        return True
    except Exception as exc:  # noqa: BLE001 - a spike records every failure kind
        print(f"{label}: failed - {type(exc).__name__}: {str(exc)[:300]}")
        return False


def chat_once(
    settings: FoundrySettings, deployment: str, *, use_responses_api: bool | None
) -> str:
    reply = chat_model(
        settings, deployment, use_responses_api=use_responses_api
    ).invoke(PROMPT)
    return repr(reply.text.strip())


def embed_once(settings: FoundrySettings) -> str:
    vector = embeddings(settings).embed_query(PROMPT)
    check_dimensions(vector)
    return f"{len(vector)} dimensions"


def rerank_once(client: ClientV2, *, max_tokens_per_doc: int | None) -> str:
    response = client.rerank(
        model="Cohere-rerank-v4.0-fast",
        query=QUERY,
        documents=DOCUMENTS,
        top_n=3,
        max_tokens_per_doc=max_tokens_per_doc,
    )
    ranking = [(r.index, round(r.relevance_score, 3)) for r in response.results]
    return f"max_tokens_per_doc={max_tokens_per_doc} accepted; ranking {ranking}"


def rerank_with(label: str, token: str | Callable[[], str], base_url: str) -> bool:
    client = ClientV2(api_key=token, base_url=base_url)
    try:
        print(f"rerank via {label}: ok - {rerank_once(client, max_tokens_per_doc=512)}")
        return True
    except ApiError as exc:
        print(f"rerank via {label}: status {exc.status_code} - {str(exc.body)[:300]}")
        if exc.status_code == 400 and "max_tokens_per_doc" in str(exc.body):
            return record(
                f"rerank via {label} without max_tokens_per_doc",
                lambda: rerank_once(client, max_tokens_per_doc=None),
            )
        return False


def main() -> int:
    settings = FoundrySettings.from_env()
    auth = "key" if settings.key else "Entra (AzureCliCredential)"
    print(f"endpoint {settings.endpoint}; chat and embeddings auth: {auth}")
    ok = [
        record(
            "gpt-5.4-mini chat",
            lambda: chat_once(settings, "gpt-5.4-mini", use_responses_api=None),
        ),
        record(
            "gpt-5.4-nano chat",
            lambda: chat_once(settings, "gpt-5.4-nano", use_responses_api=None),
        ),
        record(
            "DeepSeek-V4-Pro chat completions",
            lambda: chat_once(settings, "DeepSeek-V4-Pro", use_responses_api=None),
        ),
        record(
            "DeepSeek-V4-Pro responses path",
            lambda: chat_once(settings, "DeepSeek-V4-Pro", use_responses_api=True),
        ),
        record("text-embedding-3-large", lambda: embed_once(settings)),
    ]
    base_url = cohere_base_url(settings.endpoint)
    entra_token = get_bearer_token_provider(AzureCliCredential(), TOKEN_SCOPE)
    reranked = rerank_with("Entra token", entra_token, base_url)
    if not reranked and settings.key:
        reranked = rerank_with("key", settings.key, base_url)
    elif not reranked:
        print("rerank via key: skipped - FOUNDRY_KEY not set")
    ok.append(reranked)
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
