"""The two sign-in paths of database.connect and the engine creator, offline."""

import asyncio
import base64
import json
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.identity import (
    AzureCliCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
    aio,
)
from azure_postgresql_auth.psycopg3 import AsyncEntraConnection, EntraConnection
from psycopg import AsyncConnection, Connection

from dispute_casework import database
from dispute_casework.database import DatabaseSettings, async_connection, connect

AZURE_URL = "postgresql://psql-dcw-cus.postgres.database.azure.com/dcw?sslmode=require"
LOCAL_URL = "postgresql://dcw_app:dcw_app@127.0.0.1:5432/dcw"
UPN = "service@example.test"
TOKEN = ".".join(
    base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
    for part in ({"alg": "none"}, {"upn": UPN})
)


class FakeCredential:
    def __init__(self, *, require_envvar: bool) -> None:
        pass

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken(TOKEN, 0)

    def __enter__(self) -> "FakeCredential":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeAsyncCredential:
    def __init__(self, *, require_envvar: bool) -> None:
        pass

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

    def record(cls: type[Any], conninfo: str, /, **kwargs: Any) -> None:
        received.update(kwargs, conninfo=conninfo, cls=cls)

    async def record_async(cls: type[Any], conninfo: str, /, **kwargs: Any) -> None:
        received.update(kwargs, conninfo=conninfo, cls=cls)

    monkeypatch.setattr(Connection, "connect", classmethod(record))
    monkeypatch.setattr(AsyncConnection, "connect", classmethod(record_async))
    monkeypatch.setattr(database, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(database, "AsyncDefaultAzureCredential", FakeAsyncCredential)
    return received


@pytest.fixture
def handed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The keyword arguments the next connect, sync or async, hands to the vendor."""
    received: dict[str, Any] = {}

    def record(cls: type[Any], conninfo: str, /, **kwargs: Any) -> None:
        received.update(kwargs)

    async def record_async(cls: type[Any], conninfo: str, /, **kwargs: Any) -> None:
        received.update(kwargs)

    monkeypatch.setattr(EntraConnection, "connect", classmethod(record))
    monkeypatch.setattr(AsyncEntraConnection, "connect", classmethod(record_async))
    return received


def test_a_url_without_a_password_signs_in_with_entra() -> None:
    assert DatabaseSettings(url=AZURE_URL).entra
    assert not DatabaseSettings(url=LOCAL_URL).entra


@pytest.mark.parametrize(
    ("selection", "sync_link", "async_link"),
    [
        ("azureclicredential", AzureCliCredential, aio.AzureCliCredential),
        (
            "managedidentitycredential",
            ManagedIdentityCredential,
            aio.ManagedIdentityCredential,
        ),
    ],
)
def test_the_environment_selects_the_one_credential_handed_to_the_vendor(
    monkeypatch: pytest.MonkeyPatch,
    handed: dict[str, Any],
    selection: str,
    sync_link: type[Any],
    async_link: type[Any],
) -> None:
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", selection)
    connect(DatabaseSettings(url=AZURE_URL))
    sync_credential = handed["credential"]
    connection_class, keywords = async_connection(DatabaseSettings(url=AZURE_URL))
    asyncio.run(connection_class.connect(AZURE_URL, **keywords))
    async_credential = handed["credential"]
    assert type(sync_credential) is DefaultAzureCredential
    assert [type(link) for link in sync_credential.credentials] == [sync_link]
    assert type(async_credential) is aio.DefaultAzureCredential
    assert [type(link) for link in async_credential.credentials] == [async_link]


def test_sign_in_refuses_to_guess_a_credential(
    monkeypatch: pytest.MonkeyPatch, handed: dict[str, Any]
) -> None:
    monkeypatch.delenv("AZURE_TOKEN_CREDENTIALS", raising=False)
    with pytest.raises(ValueError, match="AZURE_TOKEN_CREDENTIALS"):
        connect(DatabaseSettings(url=AZURE_URL))
    connection_class, keywords = async_connection(DatabaseSettings(url=AZURE_URL))
    with pytest.raises(ValueError, match="AZURE_TOKEN_CREDENTIALS"):
        asyncio.run(connection_class.connect(AZURE_URL, **keywords))
    assert handed == {}


def test_entra_connect_takes_user_and_password_from_the_token(
    connected: dict[str, Any],
) -> None:
    connect(DatabaseSettings(url=AZURE_URL), options="-c role=tier_full")
    assert connected == {
        "cls": EntraConnection,
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
    assert keywords == {"user": group}


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
    connection_class, keywords = async_connection(DatabaseSettings(url=AZURE_URL))
    asyncio.run(
        connection_class.connect(AZURE_URL, options="-c role=lg_app", **keywords)
    )
    assert connected == {
        "cls": database.AsyncTokenConnection,
        "conninfo": AZURE_URL,
        "user": UPN,
        "password": TOKEN,
        "options": "-c role=lg_app",
    }


def test_password_creator_has_no_credential() -> None:
    assert async_connection(DatabaseSettings(url=LOCAL_URL)) == (AsyncConnection, {})
