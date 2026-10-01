import sys

import pytest
from azure.identity import AzureCliCredential

from dispute_casework import foundry_smoke
from dispute_casework.foundry import (
    FoundrySettings,
    chat_model,
    cohere_base_url,
    credential,
    embeddings,
)

ENDPOINT = "https://ai-test-eus2.cognitiveservices.azure.com"


def test_settings_require_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_ENDPOINT", raising=False)
    with pytest.raises(KeyError):
        FoundrySettings.from_env()


def test_settings_strip_trailing_slash_and_treat_empty_key_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_ENDPOINT", f"{ENDPOINT}/")
    monkeypatch.setenv("FOUNDRY_KEY", "")
    assert FoundrySettings.from_env() == FoundrySettings(endpoint=ENDPOINT)


def test_credential_is_the_key_when_set() -> None:
    assert credential(FoundrySettings(endpoint=ENDPOINT, key="k")) == "k"


def test_credential_is_cli_identity_without_key() -> None:
    assert isinstance(
        credential(FoundrySettings(endpoint=ENDPOINT)), AzureCliCredential
    )


def test_cohere_base_url_is_the_provider_route() -> None:
    assert (
        cohere_base_url(FoundrySettings(endpoint=ENDPOINT))
        == "https://ai-test-eus2.cognitiveservices.azure.com/providers/cohere"
    )


def test_deepseek_uses_chat_completions_and_openai_models_use_responses() -> None:
    settings = FoundrySettings(endpoint=ENDPOINT, key="k")
    assert chat_model(settings, "DeepSeek-V4-Pro").use_responses_api is False
    assert chat_model(settings, "gpt-5.4-mini").use_responses_api is True


def test_embeddings_send_raw_strings_at_1536_dimensions() -> None:
    model = embeddings(FoundrySettings(endpoint=ENDPOINT, key="k"))
    assert model.check_embedding_ctx_length is False
    assert model.dimensions == 1536


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
