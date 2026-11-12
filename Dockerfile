FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

RUN useradd --no-create-home --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["uvicorn", "dispute_casework.api:create_app", "--factory", "--host", "0.0.0.0"]
CMD ["--port", "8000"]
