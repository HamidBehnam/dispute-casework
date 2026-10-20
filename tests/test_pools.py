"""The pools: the budget, the login's connection limit and the role reset on checkin."""

import asyncio
from contextlib import ExitStack
from functools import partial

import psycopg
import pytest
from psycopg.rows import DictRow
from sqlalchemy import text

from dispute_casework import database as pools
from dispute_casework.database import (
    CONNECTION_LIMIT,
    DatabaseSettings,
    checkpointer_pool,
    connect,
    engine,
    tier_engines,
)


def test_startup_refuses_pools_over_the_connection_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pools.check_pool_budget()
    monkeypatch.setattr(pools, "CHECKPOINTER_POOL_SIZE", CONNECTION_LIMIT)
    settings = DatabaseSettings(url="postgresql://unused:unused@127.0.0.1:5432/unused")
    for start in (
        pools.check_pool_budget,
        partial(tier_engines, settings),
        partial(checkpointer_pool, settings),
    ):
        with pytest.raises(RuntimeError, match=f"allows {CONNECTION_LIMIT}"):
            start()


def test_the_login_refuses_a_seventeenth_connection(database: DatabaseSettings) -> None:
    with ExitStack() as held:
        first = held.enter_context(connect(database))
        open_already = first.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = current_user"
        ).fetchone()
        assert open_already is not None
        for _ in range(CONNECTION_LIMIT - open_already[0]):
            held.enter_context(connect(database))
        with pytest.raises(psycopg.OperationalError, match="too many connections"):
            connect(database)


def backend_states(database: DatabaseSettings, application_name: str) -> list[str]:
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT state FROM pg_stat_activity WHERE application_name = %s",
            (application_name,),
        ).fetchall()
    return [state for (state,) in rows]


@pytest.mark.parametrize(
    "first_checkout_ends_with",
    [
        "the commit",
        "a statement after the commit",
        "a driver-level statement left open after the commit",
    ],
)
def test_a_tier_engine_connection_returns_to_its_bound_role(
    database: DatabaseSettings, first_checkout_ends_with: str
) -> None:
    """One connection in the pool, so the second checkout gets the first one back.

    The switch is committed: an uncommitted SET ROLE is undone by the rollback
    at checkin and would prove nothing.
    """
    bound = engine(
        database,
        pool_size=1,
        max_overflow=0,
        options="-c role=tier_public -c application_name=test_pools",
    )
    # The vendor exposes the AsyncEngine behind a PGEngine as _pool only.
    async_engine = bound._pool

    async def switch_then_check_out_again() -> tuple[str, str, str, list[str]]:
        async with async_engine.connect() as connection:
            await connection.execute(text("SET ROLE tier_full"))
            await connection.commit()
            if first_checkout_ends_with == "a statement after the commit":
                await connection.execute(text("SELECT current_user"))
            if first_checkout_ends_with.startswith("a driver-level statement"):
                driver = (await connection.get_raw_connection()).driver_connection
                assert driver is not None
                await driver.execute("SET application_name = 'leftover'")
        states = backend_states(database, "test_pools")
        async with async_engine.connect() as connection:
            returned = await connection.execute(text("SELECT current_user"))
            returned_to: str = returned.scalar_one()
            await connection.rollback()
            held = await connection.execute(text("SELECT current_user"))
            held_after_rollback: str = held.scalar_one()
            name = await connection.execute(
                text("SELECT current_setting('application_name')")
            )
            application_name: str = name.scalar_one()
        states += backend_states(database, "test_pools")
        await async_engine.dispose()
        return returned_to, held_after_rollback, application_name, states

    assert asyncio.run(switch_then_check_out_again()) == (
        "tier_public",
        "tier_public",
        "test_pools",
        ["idle", "idle"],
    )


def test_a_checkpointer_connection_returns_to_its_bound_role(
    database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One connection in the pool, so the second checkout gets the first one back.
    monkeypatch.setattr(pools, "CHECKPOINTER_POOL_SIZE", 1)

    async def switch_then_check_out_again() -> tuple[str, str]:
        async with checkpointer_pool(database) as pool:
            async with pool.connection() as connection:
                await connection.execute("SET ROLE tier_full")
                switched = await connection.execute("SELECT current_user")
                switched_to: DictRow | None = await switched.fetchone()
            async with pool.connection() as connection:
                returned = await connection.execute("SELECT current_user")
                returned_to: DictRow | None = await returned.fetchone()
        assert switched_to is not None and returned_to is not None
        return switched_to["current_user"], returned_to["current_user"]

    assert asyncio.run(switch_then_check_out_again()) == ("tier_full", "lg_app")
