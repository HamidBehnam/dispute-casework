"""checkpointer

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 01:19:42.998532

"""

from collections.abc import Callable
from typing import Any

from langchain_postgres import PGEngine
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection

from dispute_casework.database import CHECKPOINTER_CONNECTION

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade(
    make_engine: Callable[[], PGEngine], connect: Callable[..., Connection[Any]]
) -> None:
    # The vendor's setup runs CREATE INDEX CONCURRENTLY, which needs autocommit,
    # on a connection bound to lg_owner so that lg_owner owns every table.
    with connect(
        options="-c role=lg_owner -c search_path=lg", **CHECKPOINTER_CONNECTION
    ) as connection:
        PostgresSaver(connection).setup()
