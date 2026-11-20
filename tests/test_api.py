import asyncio
import secrets
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.routing import Route

from dispute_casework import api
from dispute_casework.api import ORIGIN_HEADER, create_app
from dispute_casework.identity import IdentitySettings

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
def environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    identity: IdentitySettings,
    jwks_url: str,
) -> None:
    monkeypatch.setenv("ORIGIN_HEADER_SECRET", SECRET)
    monkeypatch.setenv("IDENTITY_TENANT_ID", identity.identity_tenant_id)
    monkeypatch.setenv("API_CLIENT_ID", identity.api_client_id)
    monkeypatch.setenv("MCP_CLIENT_ID", identity.mcp_client_id)
    monkeypatch.setattr(api, "jwks_uri", lambda tenant_id: jwks_url)
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<title>index</title>")
    (tmp_path / "redirect.html").write_text("<title>Signing in</title>")
    (tmp_path / "assets" / "main.js").write_text("export {};")
    monkeypatch.setattr(api, "STATIC_DIR", tmp_path)


def test_a_request_without_the_origin_header_is_refused() -> None:
    response = get({"seconds": "1", "silent": "true"}, {})
    assert response.status_code == 403


def test_a_request_with_a_wrong_origin_header_is_refused() -> None:
    response = get({"seconds": "1", "silent": "true"}, {ORIGIN_HEADER: "other"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("header", "status"), [("other", 403), (SECRET, 200)], ids=["refused", "accepted"]
)
def test_the_origin_header_is_compared_with_compare_digest(
    monkeypatch: pytest.MonkeyPatch, header: str, status: int
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    compare_digest = secrets.compare_digest

    def recording_compare_digest(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return compare_digest(a, b)

    monkeypatch.setattr(secrets, "compare_digest", recording_compare_digest)
    response = get({"seconds": "1", "silent": "true"}, {ORIGIN_HEADER: header})
    assert response.status_code == status
    assert calls == [(header.encode(), SECRET.encode())]


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


ROUTE_PATHS = {
    "/stream": "/stream",
    "/": "/",
    "/redirect": "/redirect",
    "/assets/{path:path}": "/assets/main.js",
    "/me": "/me",
}


def test_every_route_refuses_a_request_without_the_origin_header() -> None:
    app = create_app()
    paths = [route.path for route in app.routes if isinstance(route, Route)]
    assert sorted(paths) == sorted(ROUTE_PATHS)
    for path in paths:
        response = get({"seconds": "1", "silent": "true"}, {}, ROUTE_PATHS[path])
        assert response.status_code == 403


def test_every_route_is_an_api_route() -> None:
    """A Starlette Route or a Mount skips the application's origin check."""
    routes = create_app().routes
    assert routes and all(isinstance(route, APIRoute) for route in routes), routes


def test_the_sign_in_page_and_its_assets_are_served() -> None:
    page = get({}, {ORIGIN_HEADER: SECRET}, "/")
    assert page.status_code == 200 and "<title>index</title>" in page.text
    asset = get({}, {ORIGIN_HEADER: SECRET}, "/assets/main.js")
    assert asset.status_code == 200 and asset.text == "export {};"
    assert get({}, {ORIGIN_HEADER: SECRET}, "/assets/missing.js").status_code == 404


def test_an_asset_path_cannot_leave_the_assets_directory() -> None:
    response = get({}, {ORIGIN_HEADER: SECRET}, "/assets/..%2Findex.html")
    assert response.status_code == 404


def test_the_redirect_bridge_is_never_cached_and_carries_no_opener_policy() -> None:
    response = get({}, {ORIGIN_HEADER: SECRET}, "/redirect")
    assert response.status_code == 200
    assert "<title>Signing in</title>" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "cross-origin-opener-policy" not in response.headers


def test_me_refuses_a_request_without_a_bearer_token() -> None:
    response = get({}, {ORIGIN_HEADER: SECRET}, "/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_me_returns_the_principal_of_a_valid_api_token(
    identity: IdentitySettings,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
) -> None:
    issued = claims(identity.api_client_id, roles=["restricted_viewer"])
    response = get(
        {}, {ORIGIN_HEADER: SECRET, "Authorization": f"Bearer {sign(issued)}"}, "/me"
    )
    assert response.status_code == 200
    assert response.json() == {"oid": issued["oid"], "roles": ["restricted_viewer"]}


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "mcp"},
        {"exp": 1},
        {"roles": ["administrator"]},
        {"oid": "not-a-guid"},
        {"scp": None},
    ],
    ids=["mcp-audience", "expired", "unknown-role", "malformed-oid", "no-scope"],
)
def test_me_refuses_an_invalid_token(
    identity: IdentitySettings,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
    overrides: dict[str, Any],
) -> None:
    if overrides.get("aud") == "mcp":
        overrides = {"aud": identity.mcp_client_id}
    token = sign(claims(identity.api_client_id, **overrides))
    response = get(
        {}, {ORIGIN_HEADER: SECRET, "Authorization": f"Bearer {token}"}, "/me"
    )
    assert response.status_code == 401


@pytest.fixture
def non_json_keys_url() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>maintenance</html>")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/keys"
    server.shutdown()


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
@pytest.mark.parametrize("keys", ["refused", "non-json"])
def test_me_answers_503_when_the_signing_keys_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
    identity: IdentitySettings,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
    non_json_keys_url: str,
    keys: str,
) -> None:
    url = non_json_keys_url
    if keys == "refused":
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{closed.getsockname()[1]}/keys"
    monkeypatch.setattr(api, "jwks_uri", lambda tenant_id: url)
    token = sign(claims(identity.api_client_id))
    response = get(
        {}, {ORIGIN_HEADER: SECRET, "Authorization": f"Bearer {token}"}, "/me"
    )
    assert response.status_code == 503


def test_me_needs_the_origin_header_even_with_a_token() -> None:
    response = get({}, {"Authorization": f"Bearer {uuid.uuid4()}"}, "/me")
    assert response.status_code == 403
