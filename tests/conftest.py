import os
import sys

# DeepEval reads these when it is imported, so they are set before any import
# that reaches it: no telemetry, no .env or key file read, nothing written.
DEEPEVAL_IMPORTED_BEFORE_SWITCHES = "deepeval" in sys.modules
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
os.environ["DEEPEVAL_FILE_SYSTEM"] = "READ_ONLY"
os.environ["DEEPEVAL_DISABLE_LEGACY_KEYFILE"] = "1"
os.environ.pop("CONFIDENT_API_KEY", None)

import hashlib
import json
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_postgres import PGEngine, PGVectorStore

from dispute_casework.corpus import SNAPSHOT_DIR, load
from dispute_casework.database import (
    CONNECTION_LIMIT,
    LOADER,
    DatabaseSettings,
    tier_engines,
)
from dispute_casework.foundry import FoundrySettings
from dispute_casework.retrieval import open_store

DATABASE = "dcw"
SUPERUSER = "postgres"
RUNNER = "dcw_runner"
APPLICATION = "dcw_app"
MIGRATION_ROLES = [
    "corpus_loader",
    "tier_public",
    "tier_full",
    "lg_app",
    "corpus_owner",
    "lg_owner",
    "bank_owner",
    "app_owner",
    "dcw_service",
]
RECORDED_ENDPOINT = "https://ai-dcw-eus2.cognitiveservices.azure.com"


def url(login: str) -> str:
    """The compose database; every local login's password is its name."""
    return f"postgresql://{login}:{login}@127.0.0.1:5432/{DATABASE}"


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


def without_rate_limit_responses(response: dict[str, Any]) -> dict[str, Any] | None:
    """Keeps a 429 out of the cassette; the SDK's retry is recorded in its place."""
    return None if response["status"]["code"] == 429 else response


@pytest.fixture(scope="session")
def deepeval_imported_before_switches() -> bool:
    return DEEPEVAL_IMPORTED_BEFORE_SWITCHES


@pytest.fixture(scope="session")
def vcr_config() -> dict[str, Any]:
    return {
        "match_on": ["method", "uri", "body"],
        "filter_headers": [
            "authorization",
            "api-key",
            "user-agent",
            "x-stainless-arch",
            "x-stainless-os",
            "x-stainless-runtime",
            "x-stainless-runtime-version",
            "x-fern-platform",
            "x-fern-runtime",
        ],
        "before_record_request": body_as_hash,
        "before_record_response": without_rate_limit_responses,
        "decode_compressed_response": True,
    }


@pytest.fixture(scope="session")
def settings(record_mode: str) -> FoundrySettings:
    """Replay needs no credential; recording uses FOUNDRY_ENDPOINT and the CLI login.

    Session-scoped so that it reads the environment before the per-test
    fixture below removes the variables.
    """
    if record_mode == "none":
        return FoundrySettings(endpoint=RECORDED_ENDPOINT, key="replay")
    return FoundrySettings()


@pytest.fixture(autouse=True)
def no_foundry_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings built from keyword arguments still read the environment."""
    monkeypatch.delenv("FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_KEY", raising=False)


@pytest.fixture(scope="session")
def database() -> DatabaseSettings:
    """A fresh database migrated by the runner; the application login holds dcw_service.

    The runner carries the attributes of the Azure administrator, the
    application login the CONNECTION LIMIT of the Azure test user.
    """
    with psycopg.connect(url(SUPERUSER), dbname="postgres", autocommit=True) as root:
        root.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        # The runner last: the memberships it granted depend on it as grantor.
        for role in (*MIGRATION_ROLES, APPLICATION, RUNNER):
            root.execute(f"DROP ROLE IF EXISTS {role}")
        root.execute(
            f"CREATE ROLE {RUNNER} LOGIN PASSWORD '{RUNNER}'"
            " NOSUPERUSER CREATEROLE CREATEDB INHERIT BYPASSRLS"
        )
        root.execute(
            f"CREATE ROLE {APPLICATION} LOGIN PASSWORD '{APPLICATION}'"
            f" CONNECTION LIMIT {CONNECTION_LIMIT}"
        )
        root.execute(f"CREATE DATABASE {DATABASE} OWNER {RUNNER}")
    with psycopg.connect(url(SUPERUSER), autocommit=True) as root:
        root.execute("CREATE EXTENSION IF NOT EXISTS vector")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=os.environ | {"DATABASE_URL": url(RUNNER)},
        cwd=Path(__file__).parents[1],
        check=True,
    )
    with psycopg.connect(url(RUNNER), autocommit=True) as runner:
        runner.execute(f"GRANT dcw_service TO {APPLICATION}")
    return DatabaseSettings(url=url(APPLICATION))


@pytest.fixture(scope="session")
def loader_grant(
    database: DatabaseSettings,
) -> Callable[[], AbstractContextManager[None]]:
    """A grant of corpus_loader to the application login, for one load only."""

    @contextmanager
    def granted() -> Iterator[None]:
        with psycopg.connect(url(RUNNER), autocommit=True) as runner:
            runner.execute(f"GRANT {LOADER} TO {APPLICATION}")
            try:
                yield
            finally:
                runner.execute(f"REVOKE {LOADER} FROM {APPLICATION}")

    return granted


@pytest.fixture(scope="session")
def engines(database: DatabaseSettings) -> dict[str, PGEngine]:
    return tier_engines(database)


@pytest.fixture(scope="session")
def store(
    database: DatabaseSettings,
    loader_grant: Callable[[], AbstractContextManager[None]],
    engines: dict[str, PGEngine],
    settings: FoundrySettings,
) -> PGVectorStore:
    """The corpus loaded by the application login, read through tier_full."""
    with loader_grant():
        load(database, SNAPSHOT_DIR, settings)
    return open_store(engines["tier_full"], settings)
