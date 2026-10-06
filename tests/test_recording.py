import json
from pathlib import Path
from typing import Any

from vcr import VCR
from vcr.request import Request

URL = "https://ai-test-eus2.cognitiveservices.azure.com/openai/v1/embeddings"


def response(code: int) -> dict[str, Any]:
    return {
        "status": {"code": code, "message": ""},
        "headers": {},
        "body": {"string": b"{}"},
    }


def test_a_rate_limit_response_is_never_stored(
    vcr_config: dict[str, Any], tmp_path: Path
) -> None:
    request = Request("POST", URL, b'{"input": ["a"], "model": "m"}', {})
    with VCR().use_cassette(
        str(tmp_path / "cassette.yaml"), record_mode="all", **vcr_config
    ) as cassette:
        cassette.append(request, response(429))
        cassette.append(request, response(200))
    assert [stored["status"]["code"] for stored in cassette.responses] == [200]


def test_body_hash_ignores_key_order_and_leaves_a_hashed_body_alone(
    vcr_config: dict[str, Any],
) -> None:
    body_as_hash = vcr_config["before_record_request"]
    first = body_as_hash(Request("POST", URL, b'{"input": ["a"], "model": "m"}', {}))
    second = body_as_hash(Request("POST", URL, b'{"model": "m", "input": ["a"]}', {}))
    assert first.body == second.body
    assert json.loads(first.body).keys() == {"sha256"}
    hashed = first.body
    assert body_as_hash(first).body == hashed
