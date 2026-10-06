from collections.abc import Callable

from azure.identity import AzureCliCredential, get_bearer_token_provider
from cohere import ClientV2
from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from langchain_azure_ai.embeddings import AzureAIOpenAIApiEmbeddingsModel
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_DEPLOYMENT = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_INPUTS_PER_REQUEST = 16
OPENAI_ROUTE_SCOPE = "https://ai.azure.com/.default"
COHERE_ROUTE_SCOPE = "https://cognitiveservices.azure.com/.default"
TIMEOUT_SECONDS = 60
# The rerank route answers a 429 with retry-after-ms and no retry-after. cohere
# 7.2.0 never waits on retry-after-ms, because its parser compares the header
# string with a number and discards the TypeError: it backs off 1, 2, 4 ...
# seconds, and seven retries outlast the deployment's one-minute window where
# the default two do not.
RERANK_MAX_RETRIES = 7
CHAT_COMPLETIONS_ONLY = frozenset({"DeepSeek-V4-Pro"})


class FoundrySettings(BaseSettings):
    """FOUNDRY_ENDPOINT and the optional FOUNDRY_KEY, from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="FOUNDRY_", env_ignore_empty=True, frozen=True
    )

    endpoint: str
    key: SecretStr | None = None

    @field_validator("endpoint")
    @classmethod
    def without_trailing_slash(cls, endpoint: str) -> str:
        return endpoint.rstrip("/")


def api_key(settings: FoundrySettings, scope: str) -> str | Callable[[], str]:
    """The account key when one is set, otherwise Entra tokens of the CLI login."""
    if settings.key:
        return settings.key.get_secret_value()
    return get_bearer_token_provider(AzureCliCredential(), scope)


# The model classes take api_key, never credential: given a token credential,
# langchain-azure-ai 1.2.10 builds the SDK client itself and drops the timeout
# and retry settings.
def chat_model(settings: FoundrySettings, deployment: str) -> AzureAIOpenAIApiChatModel:
    return AzureAIOpenAIApiChatModel(
        endpoint=f"{settings.endpoint}/openai/v1",
        api_key=api_key(settings, OPENAI_ROUTE_SCOPE),
        model=deployment,
        use_responses_api=deployment not in CHAT_COMPLETIONS_ONLY,
        timeout=TIMEOUT_SECONDS,
    )


def embeddings(settings: FoundrySettings) -> AzureAIOpenAIApiEmbeddingsModel:
    return AzureAIOpenAIApiEmbeddingsModel(
        endpoint=f"{settings.endpoint}/openai/v1",
        api_key=api_key(settings, OPENAI_ROUTE_SCOPE),
        model=EMBEDDING_DEPLOYMENT,
        dimensions=EMBEDDING_DIMENSIONS,
        check_embedding_ctx_length=False,
        chunk_size=EMBEDDING_INPUTS_PER_REQUEST,
        timeout=TIMEOUT_SECONDS,
    )


def reranker(settings: FoundrySettings) -> ClientV2:
    return ClientV2(
        api_key=api_key(settings, COHERE_ROUTE_SCOPE),
        base_url=f"{settings.endpoint}/providers/cohere",
        max_retries=RERANK_MAX_RETRIES,
    )
