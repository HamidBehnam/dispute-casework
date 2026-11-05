from collections.abc import Iterator

import httpx
import pytest

from dispute_casework import topology_smoke
from dispute_casework.api import ORIGIN_HEADER
from dispute_casework.topology_smoke import measure

HEADERS_AT = 1.0


class Clock:
    """Stands in for the time module inside topology_smoke; the handler moves it."""

    now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(topology_smoke, "time", clock)
    return clock


def transport(
    clock: Clock,
    chunks: list[tuple[float, bytes]],
    ends_at: float,
    error: Exception | None = None,
    status: int = 200,
) -> httpx.MockTransport:
    def body() -> Iterator[bytes]:
        for at, data in chunks:
            clock.now = at
            yield data
        clock.now = ends_at
        if error is not None:
            raise error

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now = HEADERS_AT
        return httpx.Response(status, content=body())

    return httpx.MockTransport(handler)


def test_the_record_holds_every_gap_including_the_one_to_the_end(
    clock: Clock,
) -> None:
    body = [(1.0, b"a" * 131), (16.0, b"b" * 20), (31.0, b"c" * 20)]
    record = measure("http://app", "s", 60, False, transport(clock, body, 60.0))
    assert record["status"] == 200
    assert record["ended_by"] == "server"
    assert record["t_headers_s"] == 1.0
    assert record["t_first_chunk_s"] == 1.0
    assert record["duration_s"] == 60.0
    assert record["bytes"] == 171
    assert record["chunks"] == [
        {"t": 1.0, "bytes": 131},
        {"t": 16.0, "bytes": 20},
        {"t": 31.0, "bytes": 20},
    ]
    assert record["max_gap_s"] == 29.0
    assert record["arm"] == "heartbeat" and record["seconds"] == 60


def test_a_silent_body_records_its_silence_not_zero(clock: Clock) -> None:
    record = measure(
        "http://app", "s", 300, True, transport(clock, [(1.0, b"start\n")], 126.2)
    )
    assert record["max_gap_s"] == 125.2
    assert record["ended_by"] == "server"


def test_every_time_is_rounded_before_the_gap_arithmetic(clock: Clock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        clock.now = 0.2604
        return httpx.Response(200, content=iter([b"start\n"]))

    record = measure("http://app", "s", 1, True, httpx.MockTransport(handler))
    assert record["t_headers_s"] == 0.26
    assert record["chunks"] == [{"t": 0.26, "bytes": 6}]
    assert record["max_gap_s"] == 0.0
    assert str(record["max_gap_s"]) == "0.0"


def test_a_non_200_status_ends_the_run_without_reading_the_body(
    clock: Clock,
) -> None:
    def unread_body() -> Iterator[bytes]:
        raise AssertionError("the body was read")
        yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now = 0.1
        return httpx.Response(403, content=unread_body())

    record = measure("http://app", "s", 300, False, httpx.MockTransport(handler))
    assert record["status"] == 403
    assert record["ended_by"] == "status"
    assert record["bytes"] == 0
    assert record["chunks"] == []
    assert record["t_first_chunk_s"] is None


@pytest.mark.parametrize(
    ("error", "ended_by"),
    [
        (httpx.RemoteProtocolError("peer closed connection"), "reset"),
        (httpx.ReadError("connection reset by peer"), "reset"),
        (httpx.ReadTimeout("timed out"), "timeout"),
    ],
)
def test_a_cut_mid_stream_names_how_the_body_ended(
    clock: Clock, error: Exception, ended_by: str
) -> None:
    body = [(1.0, b"a" * 131), (16.0, b"b" * 20)]
    record = measure("http://app", "s", 300, False, transport(clock, body, 90.3, error))
    assert record["ended_by"] == ended_by
    assert record["status"] == 200
    assert record["duration_s"] == 90.3
    assert record["bytes"] == 151
    assert record["max_gap_s"] == 74.3


def test_a_failed_connection_leaves_the_timing_fields_empty(clock: Clock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        clock.now = 30.0
        raise httpx.ConnectError("refused")

    record = measure("http://app", "s", 300, False, httpx.MockTransport(handler))
    assert record["ended_by"] == "connect"
    assert record["status"] is None
    assert record["t_headers_s"] is None
    assert record["max_gap_s"] is None
    assert record["duration_s"] == 30.0


def test_the_request_carries_the_origin_header_and_the_arm(clock: Clock) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=iter([b"0\n"]))

    measure("http://app", "the-secret", 300, True, httpx.MockTransport(handler))
    assert seen[0].url == "http://app/stream?seconds=300&silent=true"
    assert seen[0].headers[ORIGIN_HEADER] == "the-secret"
