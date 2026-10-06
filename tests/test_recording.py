import json
from typing import Any

from conftest import body_as_hash, without_rate_limit_responses
from vcr.request import Request

URL = "https://ai-test-eus2.cognitiveservices.azure.com/openai/v1/embeddings"


def response(code: int) -> dict[str, Any]:
    return {"status": {"code": code, "message": ""}, "headers": {}, "body": {}}


def test_a_rate_limit_response_is_never_stored(vcr_config: dict[str, Any]) -> None:
    assert vcr_config["before_record_response"] is without_rate_limit_responses
    assert without_rate_limit_responses(response(429)) is None
    assert without_rate_limit_responses(response(200)) == response(200)


def test_body_hash_ignores_key_order_and_leaves_a_hashed_body_alone() -> None:
    first = body_as_hash(Request("POST", URL, b'{"input": ["a"], "model": "m"}', {}))
    second = body_as_hash(Request("POST", URL, b'{"model": "m", "input": ["a"]}', {}))
    assert first.body == second.body
    assert json.loads(first.body).keys() == {"sha256"}
    hashed = first.body
    assert body_as_hash(first).body == hashed
