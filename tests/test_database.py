"""The two sign-in paths of database.connect and the engine creator, offline."""

import asyncio
import base64
import json
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.identity import (
    AzureCliCredential,
    ChainedTokenCredential,
    ManagedIdentityCredential,
    aio,
)
from psycopg import AsyncConnection, Connection

from dispute_casework import database
from dispute_casework.database import (
    DatabaseSettings,
    async_connection,
    async_credential,
    connect,
    credential,
)

AZURE_URL = "postgresql://psql-dcw-eus2.postgres.database.azure.com/dcw?sslmode=require"
LOCAL_URL = "postgresql://dcw_app:dcw_app@127.0.0.1:5432/dcw"
UPN = "service@example.test"
CLIENT_ID = "00000000-0000-0000-0000-000000000000"
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


def test_the_cli_login_signs_in_until_a_managed_identity_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    assert isinstance(credential(DatabaseSettings(url=AZURE_URL)), AzureCliCredential)
    assert isinstance(
        async_credential(DatabaseSettings(url=AZURE_URL)), aio.AzureCliCredential
    )


def test_a_configured_managed_identity_is_tried_before_the_cli_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", CLIENT_ID)
    chain = credential(DatabaseSettings(url=AZURE_URL))
    assert isinstance(chain, ChainedTokenCredential)
    assert [type(link) for link in chain.credentials] == [
        ManagedIdentityCredential,
        AzureCliCredential,
    ]
    async_chain = async_credential(DatabaseSettings(url=AZURE_URL))
    assert isinstance(async_chain, aio.ChainedTokenCredential)
    assert [type(link) for link in async_chain.credentials] == [
        aio.ManagedIdentityCredential,
        aio.AzureCliCredential,
    ]


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


def test_a_user_named_in_the_url_reaches_psycopg_as_a_keyword(
    connected: dict[str, Any],
) -> None:
    group = "psql-dcw-eus2-administrators"
    url = AZURE_URL.replace("://", f"://{group}@")
    connect(DatabaseSettings(url=url))
    assert connected["user"] == group
    assert connected["password"] == TOKEN
    _, keywords = async_connection(DatabaseSettings(url=url))
    assert keywords["user"] == group


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
