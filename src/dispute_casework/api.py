"""HTTP surface of the topology spike: a streamed response behind the origin check."""

import asyncio
import secrets
from typing import Annotated

from assistant_stream import RunController, create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ORIGIN_HEADER = "X-Origin-Secret"
ORIGIN_SECRET_MIN_LENGTH = 32
HEARTBEAT_SECONDS = 15
PAYLOAD_INTERVAL_SECONDS = 30
DEFAULT_STREAM_SECONDS = 300
MAX_STREAM_SECONDS = 600


class ApiSettings(BaseSettings):
    """ORIGIN_HEADER_SECRET, from the environment."""

    model_config = SettingsConfigDict(frozen=True, hide_input_in_errors=True)

    origin_header_secret: SecretStr = Field(min_length=ORIGIN_SECRET_MIN_LENGTH)


def create_app() -> FastAPI:
    expected = ApiSettings().origin_header_secret.get_secret_value().encode()

    def require_origin_header(
        secret: Annotated[str, Header(alias=ORIGIN_HEADER)] = "",
    ) -> None:
        if not secrets.compare_digest(secret.encode(), expected):
            raise HTTPException(status_code=403)

    app = FastAPI(dependencies=[Depends(require_origin_header)], openapi_url=None)

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
