"""Connections to the one Postgres database.

Every connection binds its role through the libpq startup option, so the
application never issues SET ROLE and a reset of the session returns to the
bound role. A DATABASE_URL without a password signs in with an Entra token:
of the managed identity named by AZURE_CLIENT_ID when one is configured, else
of the Azure CLI login.
"""

from functools import partial
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity import (
    AzureCliCredential,
    ChainedTokenCredential,
    ManagedIdentityCredential,
)
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from azure.identity.aio import ChainedTokenCredential as AsyncChainedTokenCredential
from azure.identity.aio import (
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
)
from azure_postgresql_auth.psycopg3 import AsyncEntraConnection, EntraConnection
from langchain_postgres import PGEngine
from psycopg import AsyncConnection, Connection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CONNECTION_LIMIT = 16
TIERS = ("tier_public", "tier_full")
TIER_POOL_SIZE = 2
TIER_MAX_OVERFLOW = 2
LOADER = "corpus_loader"
CHECKPOINTER_OPTIONS = "-c role=lg_app -c search_path=lg"
CHECKPOINTER_POOL_SIZE = 4


class DatabaseSettings(BaseSettings):
    """DATABASE_URL as a libpq connection URI and the optional AZURE_CLIENT_ID."""

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_", env_ignore_empty=True, frozen=True
    )

    url: SecretStr
    managed_identity_client_id: str | None = Field(
        default=None, validation_alias="AZURE_CLIENT_ID"
    )

    @property
    def entra(self) -> bool:
        return "password" not in conninfo_to_dict(self.url.get_secret_value())


def credential(settings: DatabaseSettings) -> TokenCredential:
    if settings.managed_identity_client_id is None:
        return AzureCliCredential()
    return ChainedTokenCredential(
        ManagedIdentityCredential(client_id=settings.managed_identity_client_id),
        AzureCliCredential(),
    )


def async_credential(settings: DatabaseSettings) -> AsyncTokenCredential:
    if settings.managed_identity_client_id is None:
        return AsyncAzureCliCredential()
    return AsyncChainedTokenCredential(
        AsyncManagedIdentityCredential(client_id=settings.managed_identity_client_id),
        AsyncAzureCliCredential(),
    )


def named_user(url: str) -> dict[str, Any]:
    """The user of the URL as a keyword; the vendor otherwise takes the token's.

    A member signing in as an Entra group presents the group's name, which no
    claim of the member's token carries.
    """
    user = conninfo_to_dict(url).get("user")
    return {"user": user} if user else {}


def connect(settings: DatabaseSettings, **kwargs: Any) -> Connection[Any]:
    url = settings.url.get_secret_value()
    if settings.entra:
        return EntraConnection.connect(
            url, credential=credential(settings), **named_user(url) | kwargs
        )
    return Connection.connect(url, **kwargs)


def async_connection(
    settings: DatabaseSettings,
) -> tuple[type[AsyncConnection[Any]], dict[str, Any]]:
    """The connection class and its keyword arguments, for a creator or a pool."""
    if settings.entra:
        url = settings.url.get_secret_value()
        return AsyncEntraConnection, {
            "credential": async_credential(settings),
            **named_user(url),
        }
    return AsyncConnection, {}


def engine(
    settings: DatabaseSettings, pool_size: int, max_overflow: int, **kwargs: Any
) -> PGEngine:
    connection_class, credential = async_connection(settings)
    creator = partial(
        connection_class.connect, settings.url.get_secret_value(), **credential | kwargs
    )
    # The URL names the dialect only; the creator carries every connection parameter.
    return PGEngine.from_connection_string(
        "postgresql+psycopg://",
        async_creator=creator,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def tier_engines(settings: DatabaseSettings) -> dict[str, PGEngine]:
    return {
        tier: engine(
            settings, TIER_POOL_SIZE, TIER_MAX_OVERFLOW, options=f"-c role={tier}"
        )
        for tier in TIERS
    }


def checkpointer_pool(
    settings: DatabaseSettings,
) -> AsyncConnectionPool[AsyncConnection[DictRow]]:
    connection_class, credential = async_connection(settings)
    return AsyncConnectionPool(
        settings.url.get_secret_value(),
        connection_class=connection_class,
        kwargs=credential
        | {
            "options": CHECKPOINTER_OPTIONS,
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=1,
        max_size=CHECKPOINTER_POOL_SIZE,
        open=False,
    )


def check_pool_budget() -> None:
    """Refuses to start when the pools could exceed the login's CONNECTION LIMIT."""
    pooled = len(TIERS) * (TIER_POOL_SIZE + TIER_MAX_OVERFLOW) + CHECKPOINTER_POOL_SIZE
    if pooled > CONNECTION_LIMIT:
        raise RuntimeError(
            f"the pools can open {pooled} connections; "
            f"the login allows {CONNECTION_LIMIT}"
        )
