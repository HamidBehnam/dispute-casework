import asyncio

import httpx
import pytest
from pydantic import ValidationError
from starlette.routing import Route

from dispute_casework import api
from dispute_casework.api import ORIGIN_HEADER, create_app

SECRET = "expected-value-padded-to-the-minimum-length"


def get(
    params: dict[str, str], headers: dict[str, str], path: str = "/stream"
) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://backend"
        ) as c:
            return await c.get(path, params=params, headers=headers)

    return asyncio.run(request())


@pytest.fixture(autouse=True)
def origin_header_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORIGIN_HEADER_SECRET", SECRET)


def test_a_request_without_the_origin_header_is_refused() -> None:
    response = get({"seconds": "1", "silent": "true"}, {})
    assert response.status_code == 403


def test_a_request_with_a_wrong_origin_header_is_refused() -> None:
    response = get({"seconds": "1", "silent": "true"}, {ORIGIN_HEADER: "other"})
    assert response.status_code == 403


def test_the_stream_carries_a_heartbeat_whenever_it_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert api.HEARTBEAT_SECONDS == 15
    # add_keepalive takes the interval in seconds; a short one keeps the test short.
    monkeypatch.setattr(api, "HEARTBEAT_SECONDS", 0.05)
    response = get({"seconds": "1"}, {ORIGIN_HEADER: SECRET})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count(": heartbeat\n\n") >= 10


def test_the_silent_arm_sends_no_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "HEARTBEAT_SECONDS", 0.05)
    response = get({"seconds": "1", "silent": "true"}, {ORIGIN_HEADER: SECRET})
    assert response.status_code == 200
    assert ": heartbeat" not in response.text
    assert "start" in response.text and "end" in response.text


def test_the_application_does_not_start_on_an_empty_or_short_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("", "x" * (api.ORIGIN_SECRET_MIN_LENGTH - 1)):
        monkeypatch.setenv("ORIGIN_HEADER_SECRET", value)
        with pytest.raises(ValidationError):
            create_app()
    monkeypatch.setenv("ORIGIN_HEADER_SECRET", "x" * api.ORIGIN_SECRET_MIN_LENGTH)
    create_app()


def test_a_rejected_secret_is_not_written_into_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = "rejected-value-shorter-than-32"
    monkeypatch.setenv("ORIGIN_HEADER_SECRET", rejected)
    with pytest.raises(ValidationError) as error:
        create_app()
    assert rejected not in str(error.value)


def test_every_route_refuses_a_request_without_the_origin_header() -> None:
    app = create_app()
    paths = [route.path for route in app.routes if isinstance(route, Route)]
    assert paths == ["/stream"]
    for path in paths:
        assert get({"seconds": "1", "silent": "true"}, {}, path).status_code == 403
