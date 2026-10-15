from contextlib import ExitStack

import psycopg
import pytest

from dispute_casework import database as pools
from dispute_casework.database import CONNECTION_LIMIT, DatabaseSettings, connect


def test_startup_refuses_pools_over_the_connection_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pools.check_pool_budget()
    monkeypatch.setattr(pools, "CHECKPOINTER_POOL_SIZE", CONNECTION_LIMIT)
    with pytest.raises(RuntimeError, match=f"allows {CONNECTION_LIMIT}"):
        pools.check_pool_budget()


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
