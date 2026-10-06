import sys

import pytest
from pydantic import ValidationError

from dispute_casework import foundry_smoke
from dispute_casework.foundry import (
    OPENAI_ROUTE_SCOPE,
    TIMEOUT_SECONDS,
    FoundrySettings,
    api_key,
    chat_model,
    embeddings,
    reranker,
)

ENDPOINT = "https://ai-test-eus2.cognitiveservices.azure.com"


def test_settings_require_endpoint() -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        FoundrySettings()


def test_settings_strip_trailing_slash_and_treat_empty_key_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_ENDPOINT", f"{ENDPOINT}/")
    monkeypatch.setenv("FOUNDRY_KEY", "")
    assert FoundrySettings() == FoundrySettings(endpoint=ENDPOINT)
    assert FoundrySettings().key is None


def test_settings_keep_the_key_out_of_their_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("FOUNDRY_KEY", "not-a-real-key")
    assert "not-a-real-key" not in repr(FoundrySettings())


def test_api_key_is_the_key_when_set() -> None:
    settings = FoundrySettings(endpoint=ENDPOINT, key="k")
    assert api_key(settings, OPENAI_ROUTE_SCOPE) == "k"


def test_api_key_is_a_token_provider_without_key() -> None:
    assert callable(api_key(FoundrySettings(endpoint=ENDPOINT), OPENAI_ROUTE_SCOPE))


@pytest.mark.parametrize("key", ["k", None])
def test_sdk_clients_carry_the_timeout_under_a_key_and_under_a_token_provider(
    key: str | None,
) -> None:
    settings = FoundrySettings(endpoint=ENDPOINT, key=key)
    chat_client = chat_model(settings, "gpt-5.4-mini").root_client
    embeddings_client = embeddings(settings).client._client
    assert chat_client.timeout == TIMEOUT_SECONDS
    assert embeddings_client.timeout == TIMEOUT_SECONDS
    assert str(embeddings_client.base_url) == f"{ENDPOINT}/openai/v1/"


def test_cohere_base_url_is_the_provider_route() -> None:
    client = reranker(FoundrySettings(endpoint=ENDPOINT, key="k"))
    assert (
        client._client_wrapper.get_base_url()
        == "https://ai-test-eus2.cognitiveservices.azure.com/providers/cohere"
    )


def test_reranker_retries_seven_times() -> None:
    client = reranker(FoundrySettings(endpoint=ENDPOINT, key="k"))
    assert client._client_wrapper.httpx_client.base_max_retries == 7


def test_deepseek_uses_chat_completions_and_openai_models_use_responses() -> None:
    settings = FoundrySettings(endpoint=ENDPOINT, key="k")
    assert chat_model(settings, "DeepSeek-V4-Pro").use_responses_api is False
    assert chat_model(settings, "gpt-5.4-mini").use_responses_api is True


def test_embeddings_send_raw_strings_at_1536_dimensions_sixteen_a_request() -> None:
    model = embeddings(FoundrySettings(endpoint=ENDPOINT, key="k"))
    assert model.check_embedding_ctx_length is False
    assert model.dimensions == 1536
    assert model.chunk_size == 16


def test_check_dimensions_accepts_1536_and_rejects_others() -> None:
    foundry_smoke.check_dimensions([0.0] * 1536)
    with pytest.raises(ValueError):
        foundry_smoke.check_dimensions([0.0] * 3072)


@pytest.fixture
def checked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def record(settings: FoundrySettings, deployment: str) -> str:
        calls.append(deployment)
        return "ready"

    monkeypatch.setenv("FOUNDRY_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(foundry_smoke, "check", record)
    return calls


def test_smoke_checks_every_deployment_by_default(
    monkeypatch: pytest.MonkeyPatch, checked: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["foundry_smoke"])
    assert foundry_smoke.main() == 0
    assert checked == list(foundry_smoke.DEPLOYMENTS)


def test_smoke_checks_only_the_named_deployment(
    monkeypatch: pytest.MonkeyPatch, checked: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["foundry_smoke", "gpt-5.4-nano"])
    assert foundry_smoke.main() == 0
    assert checked == ["gpt-5.4-nano"]


def test_smoke_rejects_an_unknown_deployment(
    monkeypatch: pytest.MonkeyPatch, checked: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["foundry_smoke", "gpt-4"])
    with pytest.raises(SystemExit):
        foundry_smoke.main()
    assert checked == []


def test_smoke_reports_a_failed_check_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_on_deepseek(settings: FoundrySettings, deployment: str) -> str:
        calls.append(deployment)
        if deployment == "DeepSeek-V4-Pro":
            raise RuntimeError("no_capacity")
        return "ready"

    monkeypatch.setenv("FOUNDRY_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(sys, "argv", ["foundry_smoke"])
    monkeypatch.setattr(foundry_smoke, "check", fail_on_deepseek)
    assert foundry_smoke.main() == 1
    assert calls == list(foundry_smoke.DEPLOYMENTS)
