"""Row-level security on corpus.chunks, per tier role bound at connect."""

import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import date

import psycopg
import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_postgres import PGEngine, PGVectorStore

from dispute_casework.corpus import SNAPSHOT_DIR, load
from dispute_casework.database import LOADER, TIERS, DatabaseSettings, connect, engine
from dispute_casework.foundry import FoundrySettings
from dispute_casework.retrieval import open_store, retrieve

LABELS = {"tier_public": {"public"}, "tier_full": {"public", "restricted"}}
ROWS_PER_LABEL = 8
THREADS = 8
SEARCHES_PER_THREAD = 25
KEYWORD = "dispute"
EMBEDDING = DeterministicFakeEmbedding(size=1536)


@pytest.fixture(scope="module")
def rows(
    database: DatabaseSettings,
    loader_grant: Callable[[], AbstractContextManager[None]],
    settings: FoundrySettings,
) -> Iterator[dict[str, set[str]]]:
    """Paragraph IDs per label of a fixture that replaces the corpus rows.

    Fewer rows than a leg fetches, so a tier's fused search returns every row
    it may see. The corpus is loaded again afterwards for the tests that need it.
    """
    labelled = [
        (f"{label}-{number}", label)
        for label in ("public", "restricted")
        for number in range(ROWS_PER_LABEL)
    ]
    texts = [
        f"{paragraph_id}: a {label} paragraph about a {KEYWORD}"
        for paragraph_id, label in labelled
    ]
    with loader_grant():
        # Rolled back at the end: the module's fixture rows stay.
        with connect(database, options=f"-c role={LOADER}") as loader:
            loader.execute("TRUNCATE corpus.chunks")
        loader_engine = engine(database, 1, 0, options=f"-c role={LOADER}")
        open_store(loader_engine, settings).add_embeddings(
            texts=texts,
            embeddings=EMBEDDING.embed_documents(texts),
            metadatas=[
                {
                    "paragraph_id": paragraph_id,
                    "part": "fixture",
                    "section": "fixture",
                    "source": "procedure",
                    "legal_status": "procedure",
                    "snapshot": date(2026, 1, 1),
                    "access_label": label,
                }
                for paragraph_id, label in labelled
            ],
            ids=[str(uuid.uuid5(uuid.NAMESPACE_URL, text)) for text in texts],
        )
    yield {
        label: {
            paragraph_id for paragraph_id, row_label in labelled if row_label == label
        }
        for label in ("public", "restricted")
    }
    with loader_grant():
        load(database, SNAPSHOT_DIR, settings)


@pytest.mark.parametrize("tier", TIERS)
def test_each_tier_sees_exactly_its_labels(
    rows: dict[str, set[str]], database: DatabaseSettings, tier: str
) -> None:
    with connect(database, options=f"-c role={tier}") as connection:
        counted = connection.execute(
            "SELECT access_label, count(*) FROM corpus.chunks GROUP BY access_label"
        ).fetchall()
    assert dict(counted) == {label: len(rows[label]) for label in LABELS[tier]}


def test_force_rls_on_protected_tables(database: DatabaseSettings) -> None:
    with connect(database, options="-c role=tier_full") as connection:
        row = connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE oid = 'corpus.chunks'::regclass"
        ).fetchone()
    assert row == (True, True)


def test_no_role_bypasses_rls(
    database: DatabaseSettings, runner: DatabaseSettings
) -> None:
    with connect(database) as connection:
        bypassing = connection.execute(
            "SELECT rolname FROM pg_roles"
            " WHERE rolbypassrls AND NOT rolsuper AND rolname NOT LIKE 'pg\\_%'"
        ).fetchall()
    assert bypassing == []
    for login in (database, runner):
        with connect(login) as connection:
            row = connection.execute(
                "SELECT rolbypassrls FROM pg_roles WHERE rolname = session_user"
            ).fetchone()
        assert row == (False,)


def test_corpus_owner_owns_the_chunks_table_and_the_loader_cannot_alter_it(
    database: DatabaseSettings,
    loader_grant: Callable[[], AbstractContextManager[None]],
) -> None:
    with loader_grant(), connect(database, options=f"-c role={LOADER}") as loader:
        owner = loader.execute(
            "SELECT tableowner FROM pg_tables"
            " WHERE schemaname = 'corpus' AND tablename = 'chunks'"
        ).fetchone()
        assert owner == ("corpus_owner",)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="owner"):
            loader.execute("ALTER TABLE corpus.chunks NO FORCE ROW LEVEL SECURITY")


def test_the_login_holds_no_corpus_privilege_without_a_role_while_granted_the_loader(
    database: DatabaseSettings,
    loader_grant: Callable[[], AbstractContextManager[None]],
) -> None:
    with loader_grant():
        with connect(database) as connection:
            inherits = connection.execute(
                "SELECT pg_has_role(current_user, %s, 'USAGE')", (LOADER,)
            ).fetchone()
            assert inherits == (False,)
            with pytest.raises(
                psycopg.errors.InsufficientPrivilege, match="schema corpus"
            ):
                connection.execute("SELECT count(*) FROM corpus.chunks")
        # Rolled back at the end: the module's fixture rows stay.
        with connect(database, options=f"-c role={LOADER}") as loader:
            loader.execute("TRUNCATE corpus.chunks")
            loader.execute(
                "INSERT INTO corpus.chunks (chunk_id, content, embedding, content_tsv,"
                " paragraph_id, part, section, source, legal_status, snapshot,"
                " access_label) VALUES (gen_random_uuid(), 'probe', %s,"
                " to_tsvector('pg_catalog.english', 'probe'), 'probe', 'fixture',"
                " 'fixture', 'procedure', 'procedure', '2026-01-01', 'public')",
                (str([0.0] * 1536),),
            )
            rows = loader.execute("SELECT count(*) FROM corpus.chunks").fetchone()
            assert rows == (1,)
            loader.rollback()


def test_the_login_can_set_exactly_the_roles_dcw_service_holds(
    database: DatabaseSettings,
) -> None:
    with connect(database) as connection:
        memberships = connection.execute(
            "SELECT member::regrole::text, roleid::regrole::text,"
            " inherit_option, set_option FROM pg_auth_members"
            " WHERE member IN (session_user::regrole, 'dcw_service'::regrole)"
            " ORDER BY 1, 2"
        ).fetchall()
        login = connection.info.user
    assert memberships == [
        (login, "dcw_service", True, True),
        ("dcw_service", "lg_app", False, True),
        ("dcw_service", "tier_full", False, True),
        ("dcw_service", "tier_public", False, True),
    ]


def test_the_login_without_a_role_cannot_read_the_corpus(
    database: DatabaseSettings,
) -> None:
    with (
        connect(database) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="schema corpus"),
    ):
        connection.execute("SELECT count(*) FROM corpus.chunks")


def test_a_role_the_login_was_not_granted_fails_at_connect(
    database: DatabaseSettings,
) -> None:
    with pytest.raises(psycopg.OperationalError, match="permission denied to set role"):
        connect(database, options="-c role=corpus_owner")


def test_concurrent_searches_return_only_the_rows_of_their_tier(
    rows: dict[str, set[str]], engines: dict[str, PGEngine], settings: FoundrySettings
) -> None:
    stores = {tier: open_store(engines[tier], settings) for tier in TIERS}
    query = EMBEDDING.embed_query(KEYWORD)

    def paragraph_ids_seen(store: PGVectorStore) -> list[set[str]]:
        seen = []
        for _ in range(SEARCHES_PER_THREAD):
            candidates = retrieve(store, query, KEYWORD)
            seen.append(
                {candidate.metadata["paragraph_id"] for candidate in candidates}
            )
        return seen

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        searches = {
            tier: [
                executor.submit(paragraph_ids_seen, stores[tier])
                for _ in range(THREADS // len(TIERS))
            ]
            for tier in TIERS
        }
    for tier, futures in searches.items():
        visible = set[str]().union(*(rows[label] for label in LABELS[tier]))
        for future in futures:
            assert future.result() == [visible] * SEARCHES_PER_THREAD
