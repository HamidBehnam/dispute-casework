import asyncio
import json
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx2
import judge_model
import judge_spike
import openai
import pytest
from azure.identity import CredentialUnavailableError
from deepeval.errors import DeepEvalError
from deepeval.evaluate import evaluate
from deepeval.metrics.faithfulness.schema import Truths, Verdicts
from judge_inputs import CHUNKS, QUERIES, answer_prompt, build_cases
from judge_model import JUDGE_MAX_RETRIES, JUDGE_TIMEOUT_SECONDS, Judge
from judge_spike import CASE_TIMEOUT_SECONDS, late_rows, run
from judge_validity import gate, measurement_outcome
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError
from request_log import RequestLog, connection_facts

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
# A reply is a status and body, "drop" (close without answering) or "reset"
# (send part of an answer, then reset the connection).
Reply = tuple[int, str] | str


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
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                server.bodies.append(json.loads(self.rfile.read(length)))
                reply = server.replies.pop(0)
                if isinstance(reply, str):
                    self.close_connection = True
                    if reply == "reset":
                        self.reset_mid_response()
                    return
                status, body = reply
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("retry-after-ms", "1")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())

            def reset_mid_response(self) -> None:
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b'Content-Length: 1000\r\n\r\n{"id"'
                )
                self.wfile.flush()
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )

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
    assert judge.chat.root_client.max_retries == JUDGE_MAX_RETRIES == 5
    assert judge.chat.root_async_client.max_retries == JUDGE_MAX_RETRIES


def test_a_chat_model_without_max_retries_keeps_the_sdk_default() -> None:
    settings = FoundrySettings(endpoint="https://ai-test.example", key="k")
    chat = foundry.chat_model(settings, "gpt-5.4-mini")
    assert chat.root_client.max_retries == openai.DEFAULT_MAX_RETRIES == 2


def test_a_schema_answer_is_parsed_and_recorded_as_valid(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [(200, completion(VALID["Truths"]))]
    assert judge_at(chat_server, log).generate(PROMPT, Truths) == Truths(truths=["a"])
    [row] = log.rows()
    assert row["outcome"] == "valid"
    assert row["step"] == "Truths"
    assert [attempt["status"] for attempt in row["attempts"]] == [200]
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
    chat_server.replies = [(429, '{"error": "busy"}')] * 6
    with pytest.raises(openai.RateLimitError):
        judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    assert [attempt["status"] for attempt in row["attempts"]] == [429] * 6
    assert row["outcome"] == "transport_error"


def statuses(row: dict[str, Any]) -> list[int | None]:
    return [attempt.get("status") for attempt in row["attempts"]]


def test_a_retry_inside_the_keep_alive_window_reuses_the_connection(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [
        (429, '{"error": "busy"}'),
        (200, completion(VALID["Truths"])),
    ]
    judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    first, retry = row["attempts"]
    assert first["new_connection"] is True
    assert retry["new_connection"] is False
    assert retry["opened_at"] is None
    assert not any(e["name"].startswith("connection.") for e in retry["events"])
    assert statuses(row) == [429, 200]


def test_a_reused_connection_dropped_before_the_answer_leaves_its_exception(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [
        (429, '{"error": "busy"}'),
        "drop",
        (200, completion(VALID["Truths"])),
    ]
    judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    assert statuses(row) == [429, None, 200]
    dropped = row["attempts"][1]
    assert dropped["new_connection"] is False
    assert dropped["failed_phase"] == "http11.receive_response_headers.failed"
    assert dropped["exception"][0]["class"] == "RemoteProtocolError"
    assert row["attempts"][2]["new_connection"] is True
    assert row["outcome"] == "valid"


def events(*names: str, failed: str | None = None) -> dict[str, Any]:
    recorded: list[dict[str, Any]] = [
        {"name": name, "at": float(at)} for at, name in enumerate(names)
    ]
    if failed:
        recorded.append(
            {
                "name": failed,
                "at": float(len(names)),
                "exception": [{"class": "X", "message": "m"}],
            }
        )
    return {"events": recorded}


TCP = ("connection.connect_tcp.started", "connection.connect_tcp.complete")
TLS = ("connection.start_tls.started", "connection.start_tls.complete")
SEND = ("http11.send_request_headers.started", "http11.send_request_headers.complete")


@pytest.mark.parametrize(
    ("attempt", "new", "opened_at", "failed_phase"),
    [
        (events(*TCP, *TLS, *SEND), True, 3.0, None),
        (
            events(*TCP, TLS[0], failed="connection.start_tls.failed"),
            True,
            None,
            "connection.start_tls.failed",
        ),
        (
            events(TCP[0], failed="connection.connect_tcp.failed"),
            True,
            None,
            "connection.connect_tcp.failed",
        ),
        (events(*TCP, *SEND), True, 1.0, None),
        (events(*SEND), False, None, None),
    ],
    ids=["https", "tls-failed", "tcp-failed", "plain-http", "reused"],
)
def test_connection_facts_follow_the_setup_events(
    attempt: dict[str, Any],
    new: bool,
    opened_at: float | None,
    failed_phase: str | None,
) -> None:
    facts = connection_facts(attempt)
    assert facts["new_connection"] is new
    assert facts["opened_at"] == opened_at
    assert facts["failed_phase"] == failed_phase


def test_the_async_path_records_its_attempts(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [
        (429, '{"error": "busy"}'),
        (200, completion(VALID["Truths"])),
    ]
    asyncio.run(judge_at(chat_server, log).a_generate(PROMPT, Truths))
    [row] = log.rows()
    assert statuses(row) == [429, 200]
    assert all(attempt["events"] for attempt in row["attempts"])
    assert row["outcome"] == "valid"


def test_a_refused_connection_leaves_each_attempt_with_its_exception(
    log: RequestLog,
) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    settings = FoundrySettings(endpoint=f"http://127.0.0.1:{port}", key="k")
    with pytest.raises(openai.APIConnectionError):
        Judge(settings, log, "q01", 1).generate(PROMPT, Truths)
    [row] = log.rows()
    assert statuses(row) == [None] * 6
    for attempt in row["attempts"]:
        assert attempt["failed_phase"] == "connection.connect_tcp.failed"
        assert attempt["exception"][0]["class"] == "ConnectError"
        assert attempt["new_connection"] is True
        assert attempt["opened_at"] is None
    assert row["outcome"] == "transport_error"


def test_a_reset_mid_response_leaves_the_attempt_with_its_exception(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = ["reset", (200, completion(VALID["Truths"]))]
    judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    reset = row["attempts"][0]
    assert reset["status"] == 200
    assert reset["failed_phase"] == "http11.receive_response_body.failed"
    # socketserver shuts the socket down before closing it, so the client sees
    # either the reset or the early end of the body, whichever arrives first.
    assert reset["exception"][0]["class"] in ("ReadError", "RemoteProtocolError")
    assert statuses(row) == [200, 200]
    assert row["outcome"] == "valid"


def test_a_traced_row_holds_no_request_header_or_key(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [
        (429, '{"error": "busy"}'),
        "drop",
        (200, completion(VALID["Truths"])),
    ]
    settings = FoundrySettings(
        endpoint=chat_server.endpoint, key="key-text-never-recorded"
    )
    Judge(settings, log, "q01", 1).generate(PROMPT, Truths)
    [row] = log.rows()
    text = json.dumps(row).lower()
    assert all(attempt["events"] for attempt in row["attempts"])
    for secret in ("key-text-never-recorded", "authorization", "bearer", "x-stainless"):
        assert secret not in text


def test_a_401_is_an_environment_failure(
    chat_server: ChatServer, log: RequestLog
) -> None:
    chat_server.replies = [(401, '{"error": "unauthorized"}')]
    with pytest.raises(openai.AuthenticationError):
        judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    assert row["attempts"][0]["status"] == 401
    assert row["outcome"] == "environment"


def test_a_failure_handling_an_answer_is_recorded_with_the_answer(
    chat_server: ChatServer, log: RequestLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_parser(content: str) -> Any:
        raise RuntimeError("parser crashed")

    monkeypatch.setattr(judge_model, "trim_and_load_json", broken_parser)
    chat_server.replies = [(200, completion(VALID["Truths"]))]
    with pytest.raises(RuntimeError):
        judge_at(chat_server, log).generate(PROMPT, Truths)
    [row] = log.rows()
    assert row["content"] == VALID["Truths"]
    assert row["finish_reason"] == "stop"
    assert [attempt["status"] for attempt in row["attempts"]] == [200]
    assert row["outcome"] == "exception_other"


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
        ([row("parse_error", "a"), row("transport_error", "b")], "x", "parse_error"),
        ([row("transport_error", "a"), row("schema_error", "b")], "x", "schema_error"),
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
        self,
        log: RequestLog,
        case_id: str,
        repeat: int,
        remeasured: bool,
        answer: Callable[[str], str],
    ) -> None:
        super().__init__(
            FoundrySettings(endpoint="http://127.0.0.1:9", key="k"),
            log,
            case_id,
            repeat,
            remeasured=remeasured,
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


Script = dict[tuple[str, int, bool], Callable[[str], str]]


def valid(step: str) -> str:
    return VALID[step]


def cut(step: str) -> str:
    raise openai.APIConnectionError(
        request=httpx2.Request("POST", "http://127.0.0.1:9")
    )


def unparsable(step: str) -> str:
    return "not json"


def refused(step: str) -> str:
    response = httpx2.Response(
        401, request=httpx2.Request("POST", "http://127.0.0.1:9")
    )
    raise openai.AuthenticationError("unauthorized", response=response, body=None)


def on(*steps: str, then: Callable[[str], str]) -> Callable[[str], str]:
    """Answers valid except at the named steps, where it answers with `then`."""
    return lambda step: then(step) if step in steps else VALID[step]


def run_scripted(
    log: RequestLog, tmp_path: Path, script: Script
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    measurements = tmp_path / "measurements.jsonl"
    verdict, slots = run(
        CASES,
        lambda case_id, repeat, remeasured: ScriptedJudge(
            log,
            case_id,
            repeat,
            remeasured,
            script.get((case_id, repeat, remeasured), valid),
        ),
        log,
        measurements,
    )
    rows = [json.loads(line) for line in measurements.read_text().splitlines()]
    return verdict, slots, rows


def counts(verdict: Any) -> tuple[int, int, int, int, str]:
    return (verdict.n, verdict.k, verdict.s, verdict.t, verdict.verdict)


def test_a_run_measures_every_slot_once_when_nothing_is_cut(
    log: RequestLog, tmp_path: Path
) -> None:
    verdict, slots, rows = run_scripted(log, tmp_path, {})
    assert counts(verdict) == (8, 8, 0, 0, "pass")
    assert len(rows) == 8
    assert not any(row["remeasured"] for row in rows + log.rows())


def test_a_slot_cut_by_transport_is_remeasured_alone_and_counts_its_final(
    log: RequestLog, tmp_path: Path
) -> None:
    script: Script = {("q01", 1, False): on("Steps", then=cut)}
    verdict, slots, rows = run_scripted(log, tmp_path, script)
    assert counts(verdict) == (8, 8, 0, 0, "pass")
    assert {
        "slot": "q01-r1-geval",
        "first": "transport_error",
        "final": "valid",
        "remeasured": True,
    } in slots
    [remeasured] = [row for row in rows if row["remeasured"]]
    assert remeasured["measurement_id"] == "q01-r1-geval-remeasured"
    assert remeasured["outcome"] == "valid"
    first = next(row for row in rows if row["measurement_id"] == "q01-r1-geval")
    assert first["outcome"] == "transport_error"
    assert {row["step"] for row in log.rows() if row["remeasured"]} == {
        "Steps",
        "ReasonScore",
    }


def test_a_slot_cut_twice_stays_transport(log: RequestLog, tmp_path: Path) -> None:
    script: Script = {
        ("q02", 2, False): on("Claims", then=cut),
        ("q02", 2, True): on("Truths", then=cut),
    }
    verdict, slots, rows = run_scripted(log, tmp_path, script)
    assert counts(verdict) == (8, 7, 0, 1, "undecided by transport")
    assert {
        "slot": "q02-r2-faithfulness",
        "first": "transport_error",
        "final": "transport_error",
        "remeasured": True,
    } in slots
    assert [row["outcome"] for row in rows if row["remeasured"]] == ["transport_error"]


def test_a_slot_cut_by_transport_then_judged_badly_counts_as_judge_failure(
    log: RequestLog, tmp_path: Path
) -> None:
    script: Script = {
        ("q01", 1, False): on("Truths", then=cut),
        ("q01", 1, True): on("Verdicts", then=unparsable),
    }
    verdict, slots, rows = run_scripted(log, tmp_path, script)
    assert counts(verdict) == (8, 7, 1, 0, "pass")
    assert {
        "slot": "q01-r1-faithfulness",
        "first": "transport_error",
        "final": "parse_error",
        "remeasured": True,
    } in slots
    assert [row["outcome"] for row in rows if row["remeasured"]] == ["parse_error"]


def test_a_judge_failure_stands_and_only_the_cut_slot_is_remeasured(
    log: RequestLog, tmp_path: Path
) -> None:
    script: Script = {
        ("q01", 1, False): lambda step: {"Claims": unparsable, "Steps": cut}.get(
            step, valid
        )(step)
    }
    verdict, slots, rows = run_scripted(log, tmp_path, script)
    assert counts(verdict) == (8, 7, 1, 0, "pass")
    assert [row["measurement_id"] for row in rows if row["remeasured"]] == [
        "q01-r1-geval-remeasured"
    ]
    assert not any(
        row["remeasured"] and row["step"] in {"Truths", "Claims"} for row in log.rows()
    )


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (
            {
                ("q01", 1, False): on("Truths", then=unparsable),
                ("q02", 2, False): on("Steps", then=unparsable),
            },
            (8, 6, 2, 0, "fail"),
        ),
        (
            {
                ("q01", 2, False): on("Truths", "Steps", then=cut),
                ("q01", 2, True): on("Steps", then=cut),
                ("q02", 1, False): on("ReasonScore", then=unparsable),
            },
            (8, 6, 1, 1, "undecided by transport"),
        ),
        (
            {
                ("q01", 1, False): on("Truths", then=cut),
                ("q02", 1, False): on("Steps", then=cut),
            },
            (8, 8, 0, 0, "pass"),
        ),
    ],
)
def test_the_gate_counts_each_slot_by_its_final_outcome(
    log: RequestLog, tmp_path: Path, script: Script, expected: tuple[Any, ...]
) -> None:
    verdict, slots, rows = run_scripted(log, tmp_path, script)
    assert len(slots) == 8
    assert counts(verdict) == expected


def test_a_judge_failure_beside_a_transport_cut_in_one_measurement_stands(
    log: RequestLog, tmp_path: Path
) -> None:
    calls = []

    def schema_refused_then_cut(step: str) -> str:
        calls.append(step)
        if step == "Truths" and len(calls) == 1:
            raise TypeError("schema not accepted")
        if step == "Truths":
            cut(step)
        return VALID[step]

    verdict, slots, rows = run_scripted(
        log, tmp_path, {("q01", 1, False): schema_refused_then_cut}
    )
    first = next(row for row in rows if row["measurement_id"] == "q01-r1-faithfulness")
    assert first["outcome"] == "hidden_re_request"
    own = [row for row in log.rows() if row["case_id"] == "q01" and row["repeat"] == 1]
    assert [row["outcome"] for row in own[:2]] == ["exception_other", "transport_error"]
    assert counts(verdict) == (8, 7, 1, 0, "pass")
    assert not any(row["remeasured"] for row in rows)


def test_an_environment_failure_stops_the_run_before_any_remeasurement(
    log: RequestLog, tmp_path: Path
) -> None:
    script: Script = {
        ("q01", 1, False): lambda step: {"Truths": refused, "Steps": cut}.get(
            step, valid
        )(step)
    }
    verdict, slots, rows = run_scripted(log, tmp_path, script)
    assert counts(verdict) == (1, 0, 0, 1, "stopped by environment, undecided")
    assert [row["outcome"] for row in rows] == ["environment", "transport_error"]
    assert not any(row["remeasured"] for row in rows + log.rows())


def test_an_environment_failure_in_a_remeasurement_stops_the_run(
    log: RequestLog, tmp_path: Path
) -> None:
    script: Script = {
        ("q01", 1, False): on("Steps", then=cut),
        ("q01", 1, True): on("Steps", then=refused),
    }
    verdict, slots, rows = run_scripted(log, tmp_path, script)
    assert counts(verdict) == (1, 1, 0, 0, "stopped by environment, undecided")
    assert [row["measurement_id"] for row in rows] == [
        "q01-r1-faithfulness",
        "q01-r1-geval",
        "q01-r1-geval-remeasured",
    ]
    assert {row["case_id"] for row in log.rows()} == {"q01"}


def test_two_cut_slots_of_one_case_are_remeasured_each_alone(
    log: RequestLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluated: list[list[str]] = []

    def counted(test_cases: Any, metrics: Any, **configs: Any) -> Any:
        evaluated.append([type(metric).__name__ for metric in metrics])
        return evaluate(test_cases, metrics, **configs)

    monkeypatch.setattr(judge_spike, "evaluate", counted)
    judges: list[ScriptedJudge] = []

    def judge_for(case_id: str, repeat: int, remeasured: bool) -> ScriptedJudge:
        first = (case_id, repeat, remeasured) == ("q01", 1, False)
        judge = ScriptedJudge(
            log,
            case_id,
            repeat,
            remeasured,
            on("Truths", "Steps", then=cut) if first else valid,
        )
        judges.append(judge)
        return judge

    verdict, slots = run(CASES, judge_for, log, tmp_path / "measurements.jsonl")
    assert counts(verdict) == (8, 8, 0, 0, "pass")
    fresh = [judge for judge in judges if judge.remeasured]
    assert len(fresh) == 2 and fresh[0] is not fresh[1]
    assert evaluated[:3] == [
        ["FaithfulnessMetric", "GEval"],
        ["FaithfulnessMetric"],
        ["GEval"],
    ]
    assert [s["slot"] for s in slots if s["remeasured"]] == [
        "q01-r1-faithfulness",
        "q01-r1-geval",
    ]


def test_a_case_timeout_is_not_remeasured_and_counts_as_transport(
    log: RequestLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE", "1")

    def slow(step: str) -> str:
        time.sleep(1.5)
        return VALID[step]

    verdict, slots, rows = run_scripted(log, tmp_path, {("q01", 1, False): slow})
    assert counts(verdict) == (8, 6, 0, 2, "undecided by transport")
    assert [(s["first"], s["remeasured"]) for s in slots[:2]] == [
        ("case_timeout", False),
        ("case_timeout", False),
    ]
    assert not any(row["remeasured"] for row in rows)


def test_the_case_budget_covers_six_requests_at_six_attempts_each() -> None:
    assert CASE_TIMEOUT_SECONDS == 6 * 6 * 1740 == 62_640


@pytest.mark.parametrize(
    ("script", "remeasured_slots", "first_and_final"),
    [
        (
            {("q01", 1, False): on("Steps", then=cut)},
            ["q01-r1-geval"],
            ("transport_error", "valid"),
        ),
        (
            {
                ("q01", 1, False): lambda step: {"Truths": refused, "Steps": cut}.get(
                    step, valid
                )(step)
            },
            [],
            ("transport_error", "transport_error"),
        ),
    ],
)
def test_the_summary_lists_only_the_slots_actually_remeasured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: Script,
    remeasured_slots: list[str],
    first_and_final: tuple[str, str],
) -> None:
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"cases": CASES}))
    paths = [tmp_path / name for name in ("rows", "measurements", "summary")]
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv(
        "DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE", str(CASE_TIMEOUT_SECONDS)
    )
    monkeypatch.setattr("sys.argv", ["judge_spike", str(inputs), *map(str, paths)])
    monkeypatch.setattr(
        judge_spike,
        "Judge",
        lambda settings, log, case_id, repeat, remeasured: ScriptedJudge(
            log,
            case_id,
            repeat,
            remeasured,
            script.get((case_id, repeat, remeasured), valid),
        ),
    )
    assert judge_spike.main() == 0
    summary = json.loads(paths[2].read_text())
    assert summary["remeasured_slots"] == remeasured_slots
    geval = next(s for s in summary["slots"] if s["slot"] == "q01-r1-geval")
    assert (geval["first"], geval["final"]) == first_and_final


def test_late_rows_compare_each_measurement_with_its_own_end(
    log: RequestLog, tmp_path: Path
) -> None:
    measurements = tmp_path / "measurements.jsonl"
    ended = [
        (False, "faithfulness", 100.0),
        (True, "faithfulness", 200.0),
        (True, "geval", 300.0),
    ]
    measurements.write_text(
        "".join(
            json.dumps(
                {
                    "case_id": "q01",
                    "repeat": 1,
                    "remeasured": r,
                    "metric": m,
                    "case_ended_at": e,
                }
            )
            + "\n"
            for r, m, e in ended
        )
    )

    def request(remeasured: bool, step: str, ended_at: float) -> dict[str, Any]:
        return {
            "case_id": "q01",
            "repeat": 1,
            "remeasured": remeasured,
            "step": step,
            "prompt_sha256": step,
            "started_at": 90.0,
            "ended_at": ended_at,
        }

    first_late = request(False, "Truths", 150.0)
    remeasured_late = request(True, "Truths", 250.0)
    geval_on_time = request(True, "Steps", 250.0)
    for row in (first_late, remeasured_late, geval_on_time):
        log.write(row)
    assert late_rows(log, measurements) == [first_late, remeasured_late]
