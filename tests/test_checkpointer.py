"""The LangGraph checkpointer on the lg_app pool over the tables of lg_owner."""

import asyncio
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from dispute_casework.database import DatabaseSettings, checkpointer_pool, connect

TABLES = [
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
]
SENTINEL = "eyJ0b2tlbi1zZW50aW5lbCI6IDF9.never-stored"
CHUNK_IDS = ["chunk-1", "chunk-2"]


async def put_through_the_pool(
    database: DatabaseSettings, config: RunnableConfig, state: dict[str, Any]
) -> CheckpointTuple | None:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = state
    checkpoint["channel_versions"] = dict.fromkeys(state, "1")
    async with checkpointer_pool(database) as pool:
        saver = AsyncPostgresSaver(pool)
        stored = await saver.aput(
            config,
            checkpoint,
            {"source": "input", "step": 0},
            checkpoint["channel_versions"],
        )
        # The same config, now carrying the checkpoint id the writes need.
        writes_config = config.copy()
        writes_config["configurable"] = config["configurable"] | stored["configurable"]
        await saver.aput_writes(writes_config, [("chunk_ids", CHUNK_IDS)], "task-1")
        return await saver.aget_tuple(stored)


def test_checkpoint_tables_are_owned_by_lg_owner(database: DatabaseSettings) -> None:
    with connect(database, options="-c role=lg_app") as connection:
        owners = connection.execute(
            "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'lg'"
        ).fetchall()
    assert dict(owners) == dict.fromkeys(TABLES, "lg_owner")


def test_put_and_get_through_the_lg_app_pool(database: DatabaseSettings) -> None:
    config: RunnableConfig = {
        "configurable": {"thread_id": "case-1", "checkpoint_ns": ""}
    }
    stored = asyncio.run(
        put_through_the_pool(database, config, {"chunk_ids": CHUNK_IDS})
    )
    assert stored is not None
    assert stored.checkpoint["channel_values"] == {"chunk_ids": CHUNK_IDS}


@pytest.mark.parametrize("location", ["configurable", "metadata"])
def test_a_configurable_value_lands_in_checkpoint_metadata_and_nowhere_else(
    database: DatabaseSettings, location: str
) -> None:
    """A value in config["configurable"] or config["metadata"] is copied into
    checkpoints.metadata.

    The state carries chunk IDs, never the token; the vendor copies the string,
    number and boolean values of both config locations into the metadata
    column. The positive control is the sentinel found there; the guarantee is
    its absence from the checkpoint, the blobs and the writes.
    """
    thread = f"case-{location}"
    config: RunnableConfig = {
        "configurable": {"thread_id": thread, "checkpoint_ns": ""}
    }
    if location == "metadata":
        config["metadata"] = {"token": SENTINEL}
    else:
        config["configurable"]["token"] = SENTINEL
    stored = asyncio.run(
        put_through_the_pool(database, config, {"chunk_ids": CHUNK_IDS})
    )
    assert stored is not None
    serde = JsonPlusSerializer()
    with connect(database, options="-c role=lg_app -c search_path=lg") as connection:
        columns = connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'lg' AND table_name = ANY(%s)",
            (TABLES,),
        ).fetchall()
        cells = {
            (table, column): [
                serde.loads_typed((kind, value)) if column == "blob" else value
                for kind, value in connection.execute(
                    f'SELECT {"type" if column == "blob" else "NULL"}, "{column}"'
                    f" FROM {table} WHERE thread_id = %s",
                    (thread,),
                ).fetchall()
            ]
            for table, column in columns
            if table != "checkpoint_migrations"
        }
    holding_sentinel = {
        cell for cell, values in cells.items() if SENTINEL in repr(values)
    }
    assert holding_sentinel == {("checkpoints", "metadata")}
    assert CHUNK_IDS in cells["checkpoint_blobs", "blob"]
    assert CHUNK_IDS in cells["checkpoint_writes", "blob"]
