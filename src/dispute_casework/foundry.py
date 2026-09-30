import os
from collections.abc import Callable
from dataclasses import dataclass

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, get_bearer_token_provider
from cohere import ClientV2
from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from langchain_azure_ai.embeddings import AzureAIOpenAIApiEmbeddingsModel

EMBEDDING_DIMENSIONS = 1536
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
CHAT_COMPLETIONS_ONLY = frozenset({"DeepSeek-V4-Pro"})


@dataclass(frozen=True)
class FoundrySettings:
    endpoint: str
    key: str | None = None

    @classmethod
    def from_env(cls) -> "FoundrySettings":
        return cls(
            endpoint=os.environ["FOUNDRY_ENDPOINT"].rstrip("/"),
            key=os.environ.get("FOUNDRY_KEY") or None,
        )


def credential(settings: FoundrySettings) -> str | TokenCredential:
    return settings.key or AzureCliCredential()


def bearer_token(settings: FoundrySettings) -> str | Callable[[], str]:
    chosen = credential(settings)
    if isinstance(chosen, str):
        return chosen
    return get_bearer_token_provider(chosen, TOKEN_SCOPE)


def chat_model(
    settings: FoundrySettings,
    deployment: str,
    *,
    use_responses_api: bool | None = None,
) -> AzureAIOpenAIApiChatModel:
    if use_responses_api is None:
        use_responses_api = deployment not in CHAT_COMPLETIONS_ONLY
    return AzureAIOpenAIApiChatModel(
        endpoint=f"{settings.endpoint}/openai/v1",
        credential=credential(settings),
        model=deployment,
        use_responses_api=use_responses_api,
    )


def embeddings(settings: FoundrySettings) -> AzureAIOpenAIApiEmbeddingsModel:
    return AzureAIOpenAIApiEmbeddingsModel(
        endpoint=f"{settings.endpoint}/openai/v1",
        credential=credential(settings),
        model="text-embedding-3-large",
        dimensions=EMBEDDING_DIMENSIONS,
    )


def cohere_base_url(endpoint: str) -> str:
    return f"{endpoint.rstrip('/')}/providers/cohere"


def reranker(settings: FoundrySettings) -> ClientV2:
    return ClientV2(
        api_key=bearer_token(settings),
        base_url=cohere_base_url(settings.endpoint),
    )
