"""The LangGraph checkpointer on the lg_app pool over the tables of lg_owner."""

import asyncio
from typing import Any

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


def test_a_configurable_value_lands_in_checkpoint_metadata_and_nowhere_else(
    database: DatabaseSettings,
) -> None:
    """A value in config["configurable"] is copied into checkpoints.metadata.

    The state carries chunk IDs, never the token; the token travels in the
    configurable, and the vendor copies every configurable value into the
    metadata column. The positive control is the sentinel found there; the
    guarantee is its absence from the checkpoint, the blobs and the writes.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": "case-2", "checkpoint_ns": "", "token": SENTINEL}
    }
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
                    ("case-2",),
                ).fetchall()
            ]
            for table, column in columns
            if table != "checkpoint_migrations"
        }
    holding_sentinel = {
        location for location, values in cells.items() if SENTINEL in repr(values)
    }
    assert holding_sentinel == {("checkpoints", "metadata")}
    assert CHUNK_IDS in cells["checkpoint_blobs", "blob"]
