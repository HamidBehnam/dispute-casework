"""HTTP surface behind the origin check: the sign-in pages, /me and a stream."""

import asyncio
import json
import secrets
from pathlib import Path
from typing import Annotated

import jwt
from assistant_stream import RunController, create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from dispute_casework.identity import (
    IdentitySettings,
    Principal,
    issuer,
    jwks_uri,
    parse_token,
)

ORIGIN_HEADER = "X-Origin-Secret"
ORIGIN_SECRET_MIN_LENGTH = 32
HEARTBEAT_SECONDS = 15
PAYLOAD_INTERVAL_SECONDS = 30
DEFAULT_STREAM_SECONDS = 300
MAX_STREAM_SECONDS = 600
STATIC_DIR = Path(__file__).parent / "static"


class ApiSettings(BaseSettings):
    """ORIGIN_HEADER_SECRET, from the environment."""

    model_config = SettingsConfigDict(frozen=True, hide_input_in_errors=True)

    origin_header_secret: SecretStr = Field(min_length=ORIGIN_SECRET_MIN_LENGTH)


def create_app() -> FastAPI:
    expected = ApiSettings().origin_header_secret.get_secret_value().encode()
    identity = IdentitySettings()
    jwks = jwt.PyJWKClient(jwks_uri(identity.identity_tenant_id))
    # check_dir=False: the pages exist only after the web build (the image's web stage).
    pages = StaticFiles(directory=STATIC_DIR, check_dir=False)
    assets = StaticFiles(directory=STATIC_DIR / "assets", check_dir=False)

    def require_origin_header(
        secret: Annotated[str, Header(alias=ORIGIN_HEADER)] = "",
    ) -> None:
        if not secrets.compare_digest(secret.encode(), expected):
            raise HTTPException(status_code=403)

    # Synchronous, so FastAPI runs it in a thread: PyJWKClient fetches keys with urllib.
    def current_principal(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
        ],
    ) -> Principal:
        unauthorized = HTTPException(
            status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
        if credentials is None:
            raise unauthorized
        try:
            return parse_token(
                credentials.credentials,
                jwks,
                issuer=issuer(identity.identity_tenant_id),
                audience=identity.api_client_id,
            )
        except (jwt.PyJWKClientConnectionError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=503) from error
        except (jwt.PyJWTError, ValidationError) as error:
            raise unauthorized from error

    app = FastAPI(dependencies=[Depends(require_origin_header)], openapi_url=None)

    @app.get("/")
    async def index(request: Request) -> Response:
        return await pages.get_response("index.html", request.scope)

    @app.get("/redirect")
    async def redirect_bridge(request: Request) -> Response:
        """MSAL's redirect bridge: never cached, and no Cross-Origin-Opener-Policy."""
        response = await pages.get_response("redirect.html", request.scope)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/assets/{path:path}")
    async def asset(path: str, request: Request) -> Response:
        return await assets.get_response(path, request.scope)

    @app.get("/me")
    def me(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        return principal

    @app.get("/stream")
    async def stream(
        seconds: Annotated[
            int, Query(ge=1, le=MAX_STREAM_SECONDS)
        ] = DEFAULT_STREAM_SECONDS,
        silent: bool = False,
    ) -> AssistantTransportResponse:
        """A chunk every 30 s with the heartbeat, or one chunk, silence, one chunk."""

        async def run(controller: RunController) -> None:
            if silent:
                controller.append_text("start\n")
                await asyncio.sleep(seconds)
                controller.append_text("end\n")
                return
            for elapsed in range(0, seconds, PAYLOAD_INTERVAL_SECONDS):
                controller.append_text(f"{elapsed}\n")
                await asyncio.sleep(min(PAYLOAD_INTERVAL_SECONDS, seconds - elapsed))

        return AssistantTransportResponse(
            create_run(run), heartbeat=False if silent else HEARTBEAT_SECONDS
        )

    return app
