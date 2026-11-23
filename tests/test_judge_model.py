import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import openai
import pytest
from deepeval.errors import DeepEvalError
from deepeval.metrics.faithfulness.schema import Truths, Verdicts
from judge_model import JUDGE_MAX_RETRIES, JUDGE_TIMEOUT_SECONDS, Judge
from pydantic import BaseModel, ValidationError

from dispute_casework.foundry import FoundrySettings

pytestmark = pytest.mark.block_network(allowed_hosts=["127.0.0.1"])

PROMPT = "Extract the truths."
TRUTHS = '{"truths": ["a"]}'


def completion(content: str) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "DeepSeek-V4-Pro",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )


class ChatServer:
    """A local chat-completions route answering a scripted list of replies."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, str]] = []
        self.bodies: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                server.bodies.append(json.loads(self.rfile.read(length)))
                status, body = server.replies.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("retry-after-ms", "1")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.http.server_port}"


@pytest.fixture
def chat_server() -> Iterator[ChatServer]:
    server = ChatServer()
    threading.Thread(target=server.http.serve_forever, daemon=True).start()
    yield server
    server.http.shutdown()


def judge_at(server: ChatServer) -> Judge:
    return Judge(FoundrySettings(endpoint=server.endpoint, key="k"))


@pytest.mark.parametrize("key", ["k", None])
def test_the_judge_client_carries_its_timeout_under_a_key_and_under_a_token_provider(
    key: str | None,
) -> None:
    judge = Judge(FoundrySettings(endpoint="https://ai-test.example", key=key))
    assert judge.chat.root_client.timeout == JUDGE_TIMEOUT_SECONDS == 360
    assert judge.chat.root_async_client.timeout == JUDGE_TIMEOUT_SECONDS
    assert judge.chat.root_client.max_retries == JUDGE_MAX_RETRIES == 8
    assert judge.chat.root_async_client.max_retries == JUDGE_MAX_RETRIES


def test_a_schema_request_asks_for_json_mode_at_temperature_zero(
    chat_server: ChatServer,
) -> None:
    chat_server.replies = [(200, completion(TRUTHS))]
    assert judge_at(chat_server).generate(PROMPT, Truths) == Truths(truths=["a"])
    assert chat_server.bodies[0]["response_format"] == {"type": "json_object"}
    assert chat_server.bodies[0]["temperature"] == 0.0
    assert "max_tokens" not in chat_server.bodies[0]


def test_an_async_schema_request_asks_for_json_mode_and_is_parsed(
    chat_server: ChatServer,
) -> None:
    chat_server.replies = [(200, completion(TRUTHS))]
    answer = asyncio.run(judge_at(chat_server).a_generate(PROMPT, Truths))
    assert answer == Truths(truths=["a"])
    assert chat_server.bodies[0]["response_format"] == {"type": "json_object"}
    assert chat_server.bodies[0]["temperature"] == 0.0
    assert "max_tokens" not in chat_server.bodies[0]


def test_a_request_gives_up_after_its_retries(chat_server: ChatServer) -> None:
    chat_server.replies = [(429, '{"error": "busy"}')] * (1 + JUDGE_MAX_RETRIES)
    with pytest.raises(openai.RateLimitError):
        judge_at(chat_server).generate(PROMPT, Truths)
    assert len(chat_server.bodies) == 1 + JUDGE_MAX_RETRIES


@pytest.mark.parametrize(
    ("content", "schema", "raised"),
    [
        ("```json\n" + TRUTHS + "\n```", Truths, None),
        ('{"truths": ["a"]', Truths, None),
        ("not json", Truths, DeepEvalError),
        ("   ", Truths, DeepEvalError),
        ('{"truths": "x"}', Truths, ValidationError),
        ('{"claims": ["a"]}', Truths, ValidationError),
        ('{"verdicts": [{"verdict": "probably"}]}', Verdicts, ValidationError),
    ],
)
def test_an_answer_is_parsed_by_deepevals_own_parse(
    chat_server: ChatServer,
    content: str,
    schema: type[BaseModel],
    raised: type[Exception] | None,
) -> None:
    chat_server.replies = [(200, completion(content))]
    judge = judge_at(chat_server)
    if raised is None:
        assert isinstance(judge.generate(PROMPT, schema), schema)
    else:
        with pytest.raises(raised):
            judge.generate(PROMPT, schema)
