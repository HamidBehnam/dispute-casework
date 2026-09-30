import pytest
from azure.identity import AzureCliCredential

from dispute_casework.foundry import (
    FoundrySettings,
    chat_model,
    cohere_base_url,
    credential,
)
from dispute_casework.foundry_smoke import check_dimensions

ENDPOINT = "https://ai-test-eus2.cognitiveservices.azure.com/"


def test_settings_require_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_ENDPOINT", raising=False)
    with pytest.raises(KeyError):
        FoundrySettings.from_env()


def test_settings_strip_trailing_slash_and_treat_empty_key_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("FOUNDRY_KEY", "")
    assert FoundrySettings.from_env() == FoundrySettings(endpoint=ENDPOINT.rstrip("/"))


def test_credential_is_the_key_when_set() -> None:
    assert credential(FoundrySettings(endpoint=ENDPOINT, key="k")) == "k"


def test_credential_is_cli_identity_without_key() -> None:
    assert isinstance(
        credential(FoundrySettings(endpoint=ENDPOINT)), AzureCliCredential
    )


def test_cohere_base_url_is_the_provider_route() -> None:
    assert (
        cohere_base_url(ENDPOINT)
        == "https://ai-test-eus2.cognitiveservices.azure.com/providers/cohere"
    )


def test_deepseek_uses_chat_completions_and_openai_models_use_responses() -> None:
    settings = FoundrySettings(endpoint=ENDPOINT, key="k")
    assert chat_model(settings, "DeepSeek-V4-Pro").use_responses_api is False
    assert chat_model(settings, "gpt-5.4-mini").use_responses_api is True


def test_check_dimensions_accepts_1536_and_rejects_others() -> None:
    check_dimensions([0.0] * 1536)
    with pytest.raises(ValueError):
        check_dimensions([0.0] * 3072)
