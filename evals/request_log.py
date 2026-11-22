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
    """HTTP clients for one chat model whose response hooks note each attempt.

    The SDK retries inside one call, so only the transport sees every attempt.
    A failed attempt's body is read in the hook because the SDK may discard the
    response before raising.
    """

    def __init__(self) -> None:
        self.current: list[dict[str, Any]] = []
        self.sync_client = openai.DefaultHttpx2Client(
            event_hooks={"response": [self._record]}
        )
        self.async_client = openai.DefaultAsyncHttpx2Client(
            event_hooks={"response": [self._arecord]}
        )

    def _record(self, response: httpx2.Response) -> None:
        if not response.is_success:
            response.read()
        self.current.append(attempt(response))

    async def _arecord(self, response: httpx2.Response) -> None:
        if not response.is_success:
            await response.aread()
        self.current.append(attempt(response))


def attempt(response: httpx2.Response) -> dict[str, Any]:
    if response.is_success:
        return {"status": response.status_code}
    return {
        "status": response.status_code,
        "headers": dict(response.headers),
        "body": response.text,
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
        "attempts": attempts,
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
