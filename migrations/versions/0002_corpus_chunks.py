"""corpus chunks

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 01:19:42.442928

"""

import asyncio
from collections.abc import Callable
from typing import Any

from alembic import op
from langchain_postgres import Column, PGEngine
from psycopg import Connection

from dispute_casework.retrieval import hybrid_config

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade(
    make_engine: Callable[[], PGEngine], connect: Callable[..., Connection[Any]]
) -> None:
    # The vendor creates the table on its own connection and commits there;
    # the autocommit block keeps that outside this revision's transaction.
    with op.get_context().autocommit_block():
        engine = make_engine()
        engine.init_vectorstore_table(
            "chunks",
            1536,
            schema_name="corpus",
            id_column="chunk_id",
            metadata_columns=[
                Column("paragraph_id", "TEXT", nullable=False),
                Column("part", "TEXT", nullable=False),
                Column("section", "TEXT", nullable=False),
                Column("source", "TEXT", nullable=False),
                Column("legal_status", "TEXT", nullable=False),
                Column("snapshot", "DATE", nullable=False),
                Column("access_label", "TEXT", nullable=False),
            ],
            store_metadata=False,
            overwrite_existing=False,
            hybrid_search_config=hybrid_config(""),
        )
        asyncio.run(engine.close())
    op.execute("ALTER TABLE corpus.chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE corpus.chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tier_public ON corpus.chunks FOR SELECT TO tier_public"
        " USING (access_label = ANY(ARRAY['public']))"
    )
    op.execute(
        "CREATE POLICY tier_full ON corpus.chunks FOR SELECT TO tier_full"
        " USING (access_label = ANY(ARRAY['public', 'restricted']))"
    )
    op.execute("ALTER TABLE corpus.chunks OWNER TO corpus_owner")
    op.execute("GRANT USAGE ON SCHEMA corpus TO tier_public, tier_full, corpus_loader")
    op.execute("GRANT SELECT ON corpus.chunks TO tier_public, tier_full")
    # UPDATE because the vendor's insert is an upsert.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, TRUNCATE ON corpus.chunks TO corpus_loader"
    )
