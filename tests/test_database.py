"""The two sign-in paths of database.connect and the engine creator, offline."""

import asyncio
import base64
import json
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from psycopg import AsyncConnection, Connection

from dispute_casework import database
from dispute_casework.database import DatabaseSettings, async_connection, connect

AZURE_URL = "postgresql://psql-dcw-eus2.postgres.database.azure.com/dcw?sslmode=require"
LOCAL_URL = "postgresql://dcw_app:dcw_app@127.0.0.1:5432/dcw"
UPN = "service@example.test"
TOKEN = ".".join(
    base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
    for part in ({"alg": "none"}, {"upn": UPN})
)


class FakeCredential:
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken(TOKEN, 0)


class FakeAsyncCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken(TOKEN, 0)

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncCredential":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The keyword arguments the next connect, sync or async, hands to psycopg."""
    received: dict[str, Any] = {}

    def record(cls: type, conninfo: str, **kwargs: Any) -> None:
        received.update(kwargs, conninfo=conninfo, cls=cls)

    async def record_async(cls: type, conninfo: str, **kwargs: Any) -> None:
        received.update(kwargs, conninfo=conninfo, cls=cls)

    monkeypatch.setattr(Connection, "connect", classmethod(record))
    monkeypatch.setattr(AsyncConnection, "connect", classmethod(record_async))
    monkeypatch.setattr(database, "AzureCliCredential", FakeCredential)
    monkeypatch.setattr(database, "AsyncAzureCliCredential", FakeAsyncCredential)
    return received


def test_a_url_without_a_password_signs_in_with_entra() -> None:
    assert DatabaseSettings(url=AZURE_URL).entra
    assert not DatabaseSettings(url=LOCAL_URL).entra


def test_entra_connect_takes_user_and_password_from_the_token(
    connected: dict[str, Any],
) -> None:
    connect(DatabaseSettings(url=AZURE_URL), options="-c role=tier_full")
    assert connected == {
        "cls": database.EntraConnection,
        "conninfo": AZURE_URL,
        "user": UPN,
        "password": TOKEN,
        "options": "-c role=tier_full",
    }


def test_password_connect_passes_the_url_through(connected: dict[str, Any]) -> None:
    connect(DatabaseSettings(url=LOCAL_URL), options="-c role=tier_full")
    assert connected == {
        "cls": Connection,
        "conninfo": LOCAL_URL,
        "options": "-c role=tier_full",
    }


def test_entra_creator_takes_user_and_password_from_the_token(
    connected: dict[str, Any],
) -> None:
    connection_class, credential = async_connection(DatabaseSettings(url=AZURE_URL))
    asyncio.run(
        connection_class.connect(AZURE_URL, options="-c role=lg_app", **credential)
    )
    assert connected == {
        "cls": database.AsyncEntraConnection,
        "conninfo": AZURE_URL,
        "user": UPN,
        "password": TOKEN,
        "options": "-c role=lg_app",
    }


def test_password_creator_has_no_credential() -> None:
    assert async_connection(DatabaseSettings(url=LOCAL_URL)) == (AsyncConnection, {})
