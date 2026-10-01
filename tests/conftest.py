import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from langchain_postgres import PGEngine, PGVectorStore

from dispute_casework.corpus import load, read_corpus
from dispute_casework.foundry import FoundrySettings

SNAPSHOT_DIR = Path(__file__).parents[1] / "corpus/ecfr/2026-09-29"
DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
RECORDED_ENDPOINT = "https://ai-dcw-eus2.cognitiveservices.azure.com"


def body_as_hash(request: Any) -> Any:
    """Replaces a request body with the sha256 of its canonical JSON.

    Cassettes then hold no query or regulation text. vcrpy runs this on the
    live request too before it looks for a match, so the body matcher compares
    hashes on replay. It can run more than once on the same request, so a body
    that is already a hash is left as it is.
    """
    if request.body:
        body = json.loads(request.body)
        if body.keys() != {"sha256"}:
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            request.body = json.dumps({"sha256": digest})
    return request


def content_type_only(response: dict[str, Any]) -> dict[str, Any]:
    response["headers"] = {
        name: value
        for name, value in response["headers"].items()
        if name.lower() == "content-type"
    }
    return response


@pytest.fixture(scope="session")
def vcr_config() -> dict[str, Any]:
    return {
        "match_on": ["method", "uri", "body"],
        "filter_headers": ["authorization", "api-key"],
        "before_record_request": body_as_hash,
        "before_record_response": content_type_only,
        "decode_compressed_response": True,
    }


@pytest.fixture(scope="session")
def settings(record_mode: str) -> FoundrySettings:
    """Replay needs no credential; recording uses FOUNDRY_ENDPOINT and the CLI login."""
    if record_mode == "none":
        return FoundrySettings(endpoint=RECORDED_ENDPOINT, key="replay")
    return FoundrySettings.from_env()


@pytest.fixture(scope="session")
def snapshot_dir() -> Path:
    return SNAPSHOT_DIR


@pytest.fixture(scope="session")
def engine() -> PGEngine:
    return PGEngine.from_connection_string(DATABASE_URL)


@pytest.fixture(scope="session")
def store(engine: PGEngine, settings: FoundrySettings) -> PGVectorStore:
    return load(engine, SNAPSHOT_DIR, settings)


@pytest.fixture(scope="session")
def corpus_vectors() -> dict[str, list[float]]:
    """Corpus embeddings by paragraph ID, used as query vectors without a model call."""
    _, chunks, vectors = read_corpus(SNAPSHOT_DIR)
    rows: npt.NDArray[np.float32] = vectors
    return {
        chunk["paragraph_id"]: row.tolist()
        for chunk, row in zip(chunks, rows, strict=True)
    }
