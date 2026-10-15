"""Connections to the one Postgres database.

Every connection binds its role through the libpq startup option, so the
application never issues SET ROLE and a reset of the session returns to the
bound role. A DATABASE_URL without a password signs in with an Entra token
of the Azure CLI login.
"""

from functools import partial
from typing import Any

from azure.identity import AzureCliCredential
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from azure_postgresql_auth.psycopg3 import AsyncEntraConnection, EntraConnection
from langchain_postgres import PGEngine
from psycopg import AsyncConnection, Connection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CONNECTION_LIMIT = 16
TIERS = ("tier_public", "tier_full")
TIER_POOL_SIZE = 2
TIER_MAX_OVERFLOW = 2
LOADER = "corpus_loader"
CHECKPOINTER_OPTIONS = "-c role=lg_app -c search_path=lg"
CHECKPOINTER_POOL_SIZE = 4


class DatabaseSettings(BaseSettings):
    """DATABASE_URL, from the environment, as a libpq connection URI."""

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_", env_ignore_empty=True, frozen=True
    )

    url: SecretStr

    @property
    def entra(self) -> bool:
        return "password" not in conninfo_to_dict(self.url.get_secret_value())


def connect(settings: DatabaseSettings, **kwargs: Any) -> Connection[Any]:
    url = settings.url.get_secret_value()
    if settings.entra:
        return EntraConnection.connect(url, credential=AzureCliCredential(), **kwargs)
    return Connection.connect(url, **kwargs)


def async_connection(
    settings: DatabaseSettings,
) -> tuple[type[AsyncConnection[Any]], dict[str, Any]]:
    """The connection class and its credential keyword, for a creator or a pool."""
    if settings.entra:
        return AsyncEntraConnection, {"credential": AsyncAzureCliCredential()}
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
