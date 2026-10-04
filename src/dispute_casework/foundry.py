import os
from dataclasses import dataclass
from typing import Self

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, get_bearer_token_provider
from cohere import ClientV2
from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from langchain_azure_ai.embeddings import AzureAIOpenAIApiEmbeddingsModel

EMBEDDING_DEPLOYMENT = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 1536
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
CHAT_COMPLETIONS_ONLY = frozenset({"DeepSeek-V4-Pro"})


@dataclass(frozen=True)
class FoundrySettings:
    endpoint: str
    key: str | None = None

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            endpoint=os.environ["FOUNDRY_ENDPOINT"].rstrip("/"),
            key=os.environ.get("FOUNDRY_KEY") or None,
        )


def credential(settings: FoundrySettings) -> str | TokenCredential:
    return settings.key or AzureCliCredential()


def chat_model(settings: FoundrySettings, deployment: str) -> AzureAIOpenAIApiChatModel:
    return AzureAIOpenAIApiChatModel(
        endpoint=f"{settings.endpoint}/openai/v1",
        credential=credential(settings),
        model=deployment,
        use_responses_api=deployment not in CHAT_COMPLETIONS_ONLY,
    )


def embeddings(settings: FoundrySettings) -> AzureAIOpenAIApiEmbeddingsModel:
    return AzureAIOpenAIApiEmbeddingsModel(
        endpoint=f"{settings.endpoint}/openai/v1",
        credential=credential(settings),
        model=EMBEDDING_DEPLOYMENT,
        dimensions=EMBEDDING_DIMENSIONS,
        check_embedding_ctx_length=False,
    )


def cohere_base_url(settings: FoundrySettings) -> str:
    return f"{settings.endpoint}/providers/cohere"


def reranker(settings: FoundrySettings) -> ClientV2:
    chosen = credential(settings)
    if isinstance(chosen, str):
        return ClientV2(api_key=chosen, base_url=cohere_base_url(settings))
    return ClientV2(
        api_key=get_bearer_token_provider(chosen, TOKEN_SCOPE),
        base_url=cohere_base_url(settings),
    )
