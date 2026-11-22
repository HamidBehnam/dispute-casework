import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from azure.identity import CredentialUnavailableError
from deepeval.errors import DeepEvalError
from deepeval.metrics.faithfulness.schema import Truths, Verdicts
from judge_inputs import CHUNKS, QUERIES, answer_prompt, build_cases
from judge_model import JUDGE_TIMEOUT_SECONDS, Judge
from judge_spike import run
from judge_validity import gate, measurement_outcome
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError
from request_log import RequestLog

from dispute_casework import foundry
from dispute_casework.foundry import FoundrySettings

pytestmark = pytest.mark.block_network(allowed_hosts=["127.0.0.1"])

PROMPT = "Extract the truths."
VALID = {
    "Truths": '{"truths": ["a"]}',
    "Claims": '{"claims": ["a"]}',
    "Verdicts": '{"verdicts": [{"verdict": "yes", "reason": "r"}]}',
    "FaithfulnessScoreReason": '{"reason": "r"}',
    "Steps": '{"steps": ["s"]}',
    "ReasonScore": '{"score": 8, "reason": "r"}',
}
Reply = tuple[int, str]


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
        self.replies: list[Reply] = []
        self.bodies: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                server.bodies.append(json.loads(self.rfile.read(length)))
                status, body = server.replies.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("retry-after-ms", "1")
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


@pytest.fixture
def log(tmp_path: Path) -> RequestLog:
    return RequestLog(tmp_path / "rows.jsonl")


def judge_at(server: ChatServer, log: RequestLog) -> Judge:
    return Judge(FoundrySettings(endpoint=server.endpoint, key="k"), log, "q01", 1)


@pytest.mark.parametrize("key", ["k", None])
def test_the_judge_client_carries_its_timeout_under_a_key_and_under_a_token_provider(
    key: str | None, log: RequestLog
) -> None:
    settings = FoundrySettings(endpoint="https://ai-test.example", key=key)
    judge = Judge(settings, log, "q01", 1)
    assert judge.chat.root_client.timeout == JUDGE_TIMEOUT_SECONDS
    assert judge.chat.root_async_client.timeout == JUDGE_TIMEOUT_SECONDS


def test_a_schema_answer_is_parsed_and_recorded_as_valid(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [(200, completion(VALID["Truths"]))]
    assert judge_at(chat_server, log).generate(PROMPT, Truths) == Truths(truths=["a"])
    [row] = log.rows()
    assert row["outcome"] == "valid"
    assert row["step"] == "Truths"
    assert row["attempts"] == [{"status": 200}]
    assert row["usage"]["prompt_tokens"] == 3
    assert chat_server.bodies[0]["response_format"] == {"type": "json_object"}
    assert chat_server.bodies[0]["temperature"] == 0.0
    assert "max_tokens" not in chat_server.bodies[0]


def test_each_attempt_of_a_retried_request_is_recorded(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [
        (429, '{"error": "busy"}'),
        (200, completion(VALID["Truths"])),
    ]
    judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    assert [attempt["status"] for attempt in row["attempts"]] == [429, 200]
    assert row["attempts"][0]["body"] == '{"error": "busy"}'
    assert row["outcome"] == "valid"


def test_retries_exhausted_end_as_a_transport_error(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [(429, '{"error": "busy"}')] * 3
    with pytest.raises(openai.RateLimitError):
        judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    assert [attempt["status"] for attempt in row["attempts"]] == [429, 429, 429]
    assert row["outcome"] == "transport_error"


def test_a_401_is_an_environment_failure(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [(401, '{"error": "unauthorized"}')]
    with pytest.raises(openai.AuthenticationError):
        judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    assert row["attempts"][0]["status"] == 401
    assert row["outcome"] == "environment"


def test_a_credential_failure_is_an_environment_failure(
    log: RequestLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_login() -> str:
        raise CredentialUnavailableError("no Azure CLI login")

    monkeypatch.setattr(foundry, "api_key", lambda settings, scope: no_login)
    judge = Judge(FoundrySettings(endpoint="http://127.0.0.1:9"), log, "q01", 1)
    with pytest.raises(CredentialUnavailableError):
        judge.generate(PROMPT, Truths)
    [row] = log.rows()
    assert row["attempts"] == []
    assert row["outcome"] == "environment"


@pytest.mark.parametrize(
    ("content", "schema", "outcome", "raised"),
    [
        ("```json\n" + VALID["Truths"] + "\n```", Truths, "valid", None),
        ('{"truths": ["a"]', Truths, "valid", None),
        ("not json", Truths, "parse_error", DeepEvalError),
        ("   ", Truths, "blank_content", DeepEvalError),
        ('{"truths": "x"}', Truths, "schema_error", ValidationError),
        ('{"claims": ["a"]}', Truths, "schema_error", ValidationError),
        (
            '{"verdicts": [{"verdict": "probably"}]}',
            Verdicts,
            "schema_error",
            ValidationError,
        ),
    ],
)
def test_each_answer_is_classed_from_deepevals_own_parse(
    chat_server: ChatServer,
    log: RequestLog,
    content: str,
    schema: type[BaseModel],
    outcome: str,
    raised: type[Exception] | None,
) -> None:
    chat_server.replies = [(200, completion(content))]
    judge = judge_at(chat_server, log)
    if raised is None:
        assert isinstance(judge.generate(PROMPT, schema), schema)
    else:
        with pytest.raises(raised):
            judge.generate(PROMPT, schema)
    [row] = log.rows()
    assert row["outcome"] == outcome
    try:
        json.loads(content)
        strict = True
    except json.JSONDecodeError:
        strict = False
    assert row["strict_json"] is strict
    assert row["repaired"] is (outcome == "valid" and not strict)


def row(outcome: str, prompt: str = "p", step: str | None = "Truths") -> dict[str, Any]:
    return {"outcome": outcome, "prompt_sha256": prompt, "step": step}


@pytest.mark.parametrize(
    ("rows", "error", "outcome"),
    [
        ([row("valid", "a"), row("valid", "b")], None, "valid"),
        (
            [row("exception_other", "a"), row("no_schema", "a", None)],
            "x",
            "hidden_re_request",
        ),
        ([row("valid", "a"), row("parse_error", "b")], "x", "parse_error"),
        ([row("valid", "a")], "Timed out after 2.00s while evaluating", "case_timeout"),
        ([], "Skipped due to case timeout.", "case_timeout"),
        ([row("transport_error", "a")], "x", "transport_error"),
        ([row("parse_error", "a"), row("environment", "b")], "x", "environment"),
        ([row("valid", "a")], "KeyError: 'truths'", "exception_other"),
    ],
)
def test_a_measurement_takes_the_class_of_its_worst_request(
    rows: list[dict[str, Any]], error: str | None, outcome: str
) -> None:
    assert measurement_outcome(rows, error) == outcome


@pytest.mark.parametrize(
    ("outcomes", "stopped", "expected"),
    [
        (["valid"] * 99 + ["parse_error"], False, (100, 99, 1, 0, "pass")),
        (
            ["valid"] * 98 + ["parse_error", "schema_error"],
            False,
            (100, 98, 2, 0, "fail"),
        ),
        (
            ["valid"] * 98 + ["parse_error", "transport_error"],
            False,
            (100, 98, 1, 1, "undecided by transport"),
        ),
        (["parse_error", "schema_error"], True, (2, 0, 2, 0, "fail")),
        (
            ["parse_error", "valid"],
            True,
            (2, 1, 1, 0, "stopped by environment, undecided"),
        ),
    ],
)
def test_the_gate_counts_against_the_measurements_it_has(
    outcomes: list[str], stopped: bool, expected: tuple[int, int, int, int, str]
) -> None:
    verdict = gate(outcomes, stopped=stopped)
    assert (verdict.n, verdict.k, verdict.s, verdict.t, verdict.verdict) == expected


def test_cases_are_the_first_25_queries_with_their_paragraphs_and_neighbours() -> None:
    cases = build_cases(QUERIES, CHUNKS)
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    with CHUNKS.open(encoding="utf-8") as file:
        chunks = [json.loads(line) for line in file]
    assert len(cases) == 25
    assert [case["input"] for case in cases] == [q["text"] for q in queries[:25]]
    position = {chunk["paragraph_id"]: i for i, chunk in enumerate(chunks)}
    first = position[queries[0]["expected"][0]]
    assert cases[0]["retrieval_context"] == [
        chunks[i]["content"] for i in (first - 1, first, first + 1)
    ]


def test_the_answer_prompt_is_the_pre_registered_text() -> None:
    case = {"input": "Q?", "retrieval_context": ["one", "two"]}
    assert answer_prompt(case) == (
        "Answer the question using only the context below. Cite the paragraph IDs "
        "you rely on in parentheses.\n\nContext:\none\n\ntwo\n\nQuestion: Q?"
    )


class ScriptedJudge(Judge):  # type: ignore[no-untyped-call]
    """The judge with its one network call replaced by a scripted answer per step."""

    def __init__(
        self, log: RequestLog, case_id: str, repeat: int, answer: Callable[[str], str]
    ) -> None:
        super().__init__(
            FoundrySettings(endpoint="http://127.0.0.1:9", key="k"),
            log,
            case_id,
            repeat,
        )
        self.answer = answer

    def complete(self, prompt: str, schema: type[BaseModel] | None) -> AIMessage:
        step = schema.__name__ if schema else "Truths"
        return AIMessage(
            content=self.answer(step), response_metadata={"finish_reason": "stop"}
        )


CASES = [
    {
        "case_id": f"q{n:02d}",
        "input": "Q?",
        "actual_output": "A (1005.6(b)(1)).",
        "retrieval_context": ["1005.6(b)(1) text"],
    }
    for n in (1, 2)
]


def test_a_run_measures_every_case_twice(log: RequestLog, tmp_path: Path) -> None:
    measurements = tmp_path / "measurements.jsonl"
    verdict = run(
        CASES,
        lambda case_id, repeat: ScriptedJudge(log, case_id, repeat, VALID.__getitem__),
        log,
        measurements,
    )
    assert (verdict.n, verdict.k, verdict.verdict) == (8, 8, "pass")
    assert len(measurements.read_text().splitlines()) == 8


def test_a_run_stops_at_the_first_environment_failure(
    log: RequestLog, tmp_path: Path
) -> None:
    def refused(step: str) -> str:
        response = httpx2.Response(
            401, request=httpx2.Request("POST", "http://127.0.0.1:9")
        )
        raise openai.AuthenticationError("unauthorized", response=response, body=None)

    verdict = run(
        CASES,
        lambda case_id, repeat: ScriptedJudge(log, case_id, repeat, refused),
        log,
        tmp_path / "measurements.jsonl",
    )
    assert (verdict.n, verdict.verdict) == (0, "stopped by environment, undecided")
    assert {row["case_id"] for row in log.rows()} == {"q01"}
