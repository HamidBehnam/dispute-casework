"""One JSON row per model request, with every HTTP attempt the SDK made for it."""

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx2
import openai
from langchain_core.messages import AIMessage


class RequestLog:
    """Appends rows to a JSON Lines file, one flushed line per request.

    A request that DeepEval stopped waiting for keeps running on its own thread
    and may write its row while the next case is already being measured.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as file:
            return [json.loads(line) for line in file]


class Attempts:
    """HTTP clients for one chat model that record every attempt the SDK makes.

    The SDK retries inside one call, so only the transport sees each attempt.
    The request hook opens an attempt and installs httpcore2's `trace`
    extension on it, which reports each connection phase as it starts,
    completes or fails, so an attempt that never gets a response still leaves
    its events and exception. The response hook completes the attempt. A
    failed answer's body is read in the hook because the SDK may discard the
    response before raising. The trace's `info` is never stored: for request
    phases it holds the request, Authorization header included.
    """

    def __init__(self) -> None:
        self.current: list[dict[str, Any]] = []
        self.sync_client = openai.DefaultHttpx2Client(
            event_hooks={"request": [self._open], "response": [self._record]}
        )
        self.async_client = openai.DefaultAsyncHttpx2Client(
            event_hooks={"request": [self._aopen], "response": [self._arecord]}
        )

    def _new_attempt(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        self.current.append({"started_at": time.time(), "events": events})
        return events

    def _open(self, request: httpx2.Request) -> None:
        events = self._new_attempt()

        def trace(name: str, info: dict[str, Any]) -> None:
            events.append(event(name, info))

        request.extensions["trace"] = trace

    async def _aopen(self, request: httpx2.Request) -> None:
        events = self._new_attempt()

        async def trace(name: str, info: dict[str, Any]) -> None:
            events.append(event(name, info))

        request.extensions["trace"] = trace

    def _record(self, response: httpx2.Response) -> None:
        if not response.is_success:
            response.read()
        self.current[-1].update(answer(response))

    async def _arecord(self, response: httpx2.Response) -> None:
        if not response.is_success:
            await response.aread()
        self.current[-1].update(answer(response))


def event(name: str, info: dict[str, Any]) -> dict[str, Any]:
    recorded: dict[str, Any] = {"name": name, "at": time.time()}
    if "exception" in info:
        recorded["exception"] = exception_chain(info["exception"])
    return recorded


def exception_chain(error: BaseException) -> list[dict[str, str]]:
    """The exception and its causes, outermost first, each once."""
    chain = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({"class": type(current).__name__, "message": str(current)})
        current = current.__cause__ or current.__context__
    return chain


def answer(response: httpx2.Response) -> dict[str, Any]:
    version = response.extensions.get("http_version", b"").decode()
    if response.is_success:
        return {"status": response.status_code, "http_version": version}
    return {
        "status": response.status_code,
        "http_version": version,
        "headers": dict(response.headers),
        "body": response.text,
    }


def connection_facts(attempt: dict[str, Any]) -> dict[str, Any]:
    """New or reused connection, when it opened, and how the attempt failed.

    An attempt opened a new connection when it has a connect_tcp event; the
    connection counts as opened at its last completed setup event (start_tls
    on https, connect_tcp otherwise). The events stay the primary record.
    """
    names = [e["name"] for e in attempt["events"]]
    setup = [
        e["at"]
        for e in attempt["events"]
        if e["name"]
        in ("connection.connect_tcp.complete", "connection.start_tls.complete")
    ]
    failed = [e for e in attempt["events"] if "exception" in e]
    protocol = next(
        (n.split(".")[0] for n in names if n.startswith(("http11.", "http2."))), None
    )
    return {
        "new_connection": "connection.connect_tcp.started" in names,
        "opened_at": setup[-1] if setup else None,
        "protocol": protocol,
        "failed_phase": failed[0]["name"] if failed else None,
        "exception": failed[0]["exception"] if failed else None,
    }


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def strict_json(content: str | None) -> bool:
    if content is None:
        return False
    try:
        json.loads(content)
    except json.JSONDecodeError:
        return False
    return True


def recorded_attempts(
    attempts: list[dict[str, Any]], error: BaseException | None
) -> list[dict[str, Any]]:
    """Each attempt with its connection facts.

    A last attempt that failed outside every traced phase has no exception in
    its events; the SDK keeps that one as the cause of the error it raises.
    """
    recorded = [attempt | connection_facts(attempt) for attempt in attempts]
    if (
        error
        and recorded
        and "status" not in recorded[-1]
        and not recorded[-1]["exception"]
    ):
        recorded[-1]["exception"] = exception_chain(error.__cause__ or error)
    return recorded


def request_row(
    *,
    case_id: str,
    repeat: int,
    step: str | None,
    prompt: str,
    started_at: float,
    attempts: list[dict[str, Any]],
    message: AIMessage | None,
    error: BaseException | None,
    outcome: str,
) -> dict[str, Any]:
    content = str(message.text) if message else None
    return {
        "case_id": case_id,
        "repeat": repeat,
        "step": step,
        "prompt_sha256": prompt_sha256(prompt),
        "started_at": started_at,
        "ended_at": time.time(),
        "attempts": recorded_attempts(attempts, error),
        "content": content,
        "finish_reason": message.response_metadata.get("finish_reason")
        if message
        else None,
        "usage": message.response_metadata.get("token_usage") if message else None,
        "usage_metadata": message.usage_metadata if message else None,
        "error": f"{type(error).__name__}: {error}" if error else None,
        "outcome": outcome,
        "strict_json": strict_json(content),
        "repaired": outcome == "valid" and not strict_json(content),
    }
