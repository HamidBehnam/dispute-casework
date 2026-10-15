"""Connections to the one Postgres database.

An application connection binds its role through the libpq startup option
and is returned to that role whenever a pool takes it back; nothing here
issues SET ROLE. The migration runner connects with no role option. A
DATABASE_URL without a password signs in with an Entra token from the
credential that AZURE_TOKEN_CREDENTIALS names.
"""

from functools import partial
from typing import Any, Self, cast

from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure_postgresql_auth.psycopg3 import AsyncEntraConnection, EntraConnection
from langchain_postgres import PGEngine
from psycopg import AsyncConnection, Connection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import AsyncAdaptedQueuePool, ConnectionPoolEntry, PoolResetState

CONNECTION_LIMIT = 16
TIERS = ("tier_public", "tier_full")
TIER_POOL_SIZE = 2
TIER_MAX_OVERFLOW = 2
LOADER = "corpus_loader"
CHECKPOINTER_OPTIONS = "-c role=lg_app -c search_path=lg"
CHECKPOINTER_POOL_SIZE = 4


class DatabaseSettings(BaseSettings):
    """DATABASE_URL as a libpq connection URI."""

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_", env_ignore_empty=True, frozen=True
    )

    url: SecretStr

    @property
    def entra(self) -> bool:
        return "password" not in conninfo_to_dict(self.url.get_secret_value())


def named_user(url: str) -> dict[str, Any]:
    """The user of the URL as a keyword; the vendor otherwise takes the token's.

    A member signing in as an Entra group presents the group's name, which no
    claim of the member's token carries.
    """
    user = conninfo_to_dict(url).get("user")
    return {"user": user} if user else {}


class AsyncTokenConnection(AsyncEntraConnection):
    """An Entra connection whose credential is opened and closed around the connect."""

    @classmethod
    async def connect(cls, *args: Any, **kwargs: Any) -> Self:
        async with AsyncDefaultAzureCredential(require_envvar=True) as credential:
            connection = await super().connect(*args, credential=credential, **kwargs)
        # The vendor annotates its connect with its own class, not Self.
        return cast(Self, connection)


def connect(settings: DatabaseSettings, **kwargs: Any) -> Connection[Any]:
    url = settings.url.get_secret_value()
    if not settings.entra:
        return Connection.connect(url, **kwargs)
    with DefaultAzureCredential(require_envvar=True) as credential:
        return EntraConnection.connect(
            url, credential=credential, **named_user(url) | kwargs
        )


def async_connection(
    settings: DatabaseSettings,
) -> tuple[type[AsyncConnection[Any]], dict[str, Any]]:
    """The connection class and its keyword arguments, for a creator or a pool."""
    if settings.entra:
        return AsyncTokenConnection, named_user(settings.url.get_secret_value())
    return AsyncConnection, {}


def reset_role_on_return(
    dbapi_connection: DBAPIConnection,
    record: ConnectionPoolEntry,
    state: PoolResetState,
) -> None:
    if state.asyncio_safe:
        dbapi_connection.cursor().execute("RESET ROLE")


class RoleBoundPool(AsyncAdaptedQueuePool):
    """A pool whose connections return to the role their startup option bound."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # SQLAlchemy 2.1 accepts asyncio pool listeners per instance only.
        event.listen(self, "reset", reset_role_on_return)


async def reset_role(connection: AsyncConnection[Any]) -> None:
    await connection.execute("RESET ROLE")


def engine(
    settings: DatabaseSettings, pool_size: int, max_overflow: int, **kwargs: Any
) -> PGEngine:
    connection_class, keywords = async_connection(settings)
    creator = partial(
        connection_class.connect, settings.url.get_secret_value(), **keywords | kwargs
    )
    # The URL names the dialect only; the creator carries every connection
    # parameter. Autocommit, because a RESET ROLE inside a transaction is undone
    # by the rollback that follows it on checkin.
    return PGEngine.from_connection_string(
        "postgresql+psycopg://",
        async_creator=creator,
        poolclass=RoleBoundPool,
        pool_size=pool_size,
        max_overflow=max_overflow,
        isolation_level="AUTOCOMMIT",
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
    connection_class, keywords = async_connection(settings)
    return AsyncConnectionPool(
        settings.url.get_secret_value(),
        connection_class=connection_class,
        kwargs=keywords
        | {
            "options": CHECKPOINTER_OPTIONS,
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        reset=reset_role,
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
