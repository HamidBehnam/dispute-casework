FROM node:26.13.1-trixie@sha256:e18c3ccdf7d71619990b0e85639f506e2028a95410999cab24cc373cbd10e0ac AS web

WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
ARG VITE_TENANT_ID
ARG VITE_WEB_CLIENT_ID
ARG VITE_API_CLIENT_ID
RUN npm run build

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY --from=web /build/src/dispute_casework/static ./src/dispute_casework/static
RUN uv sync --locked --no-default-groups --no-editable

RUN useradd --no-create-home --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["uvicorn", "dispute_casework.api:create_app", "--factory", "--host", "0.0.0.0"]
CMD ["--port", "8000"]
