from unittest.mock import Mock, patch

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_postgres import PGEngine, PGVectorStore
from langchain_postgres.v2.hybrid_search_config import reciprocal_rank_fusion

from dispute_casework import retrieval
from dispute_casework.foundry import FoundrySettings
from dispute_casework.retrieval import (
    CANDIDATES,
    ID_COLUMN,
    METADATA_COLUMNS,
    TABLE,
    embed,
    hybrid_config,
    rerank,
    retrieve,
)

QUERY_VECTOR_OF = "1005.11(c)(1)"


def test_hybrid_config_names_the_stored_column_and_rrf() -> None:
    config = hybrid_config("provisional credit")
    assert config.tsv_column == "content_tsv"
    assert config.fts_query == "provisional credit"
    assert config.fusion_function is reciprocal_rank_fusion
    assert config.fusion_function_parameters == {"rrf_k": 60}
    assert (config.primary_top_k, config.secondary_top_k) == (40, 40)


def test_hybrid_config_built_per_call() -> None:
    store = Mock()
    retrieve(store, "first question", [0.0], "first keywords")
    retrieve(store, "second question", [0.0], "second keywords")
    first, second = (
        call.kwargs["hybrid_search_config"]
        for call in store.similarity_search_by_vector.call_args_list
    )
    assert first is not second
    assert (first.fts_query, second.fts_query) == ("first keywords", "second keywords")
    assert all(
        call.kwargs["k"] == CANDIDATES
        for call in store.similarity_search_by_vector.call_args_list
    )


@pytest.mark.parametrize("keywords", ["", "  \n"])
def test_retrieve_rejects_an_empty_keyword_string(keywords: str) -> None:
    store = Mock()
    with pytest.raises(ValueError, match="keyword string"):
        retrieve(store, "question", [0.0], keywords)
    store.similarity_search_by_vector.assert_not_called()


def test_sequential_searches_use_their_own_keyword_query(
    store: PGVectorStore, corpus_vectors: dict[str, list[float]]
) -> None:
    vector = corpus_vectors[QUERY_VECTOR_OF]
    with patch.object(
        retrieval, "reciprocal_rank_fusion", wraps=reciprocal_rank_fusion
    ) as fusion:
        retrieve(store, "first question", vector, "provisional credit")
        retrieve(store, "second question", vector, "remittance transfer")
    first_leg, second_leg = (call.args[1] for call in fusion.call_args_list)
    assert first_leg and second_leg
    assert all("provisional" in row["content"].lower() for row in first_leg)
    assert all("remittance" in row["content"].lower() for row in second_leg)
    assert {row["paragraph_id"] for row in first_leg} != {
        row["paragraph_id"] for row in second_leg
    }


def test_langchain_postgres_still_writes_the_query_into_a_shared_config(
    engine: PGEngine, store: PGVectorStore
) -> None:
    text_search_store = PGVectorStore.create_sync(
        engine,
        DeterministicFakeEmbedding(size=1536),
        TABLE,
        id_column=ID_COLUMN,
        metadata_columns=METADATA_COLUMNS,
    )
    shared = hybrid_config("")
    text_search_store.similarity_search("first", k=1, hybrid_search_config=shared)
    assert shared.fts_query == "first", (
        "langchain-postgres no longer writes the query into the HybridSearchConfig "
        "it is given (issue 337 is fixed in this version). The per-call config in "
        "retrieval.hybrid_config and its INVARIANTS.md row can be reviewed."
    )


def test_metadata_filter_constrains_both_legs(
    store: PGVectorStore, corpus_vectors: dict[str, list[float]]
) -> None:
    vector = corpus_vectors[QUERY_VECTOR_OF]

    def sources_per_leg(filter: dict[str, str] | None) -> list[set[str]]:
        fusion = Mock(wraps=reciprocal_rank_fusion)
        config = hybrid_config("error resolution")
        config.fusion_function = fusion
        store.similarity_search_by_vector(
            vector, k=CANDIDATES, filter=filter, hybrid_search_config=config
        )
        return [{row["source"] for row in leg} for leg in fusion.call_args.args[:2]]

    assert sources_per_leg(None) == [
        {"regulation", "commentary"},
        {"regulation", "commentary"},
    ]
    assert sources_per_leg({"source": "commentary"}) == [
        {"commentary"},
        {"commentary"},
    ]


def test_retrieve_returns_twenty_candidates_carrying_their_paragraph_id(
    store: PGVectorStore, corpus_vectors: dict[str, list[float]]
) -> None:
    candidates = retrieve(
        store, "question", corpus_vectors[QUERY_VECTOR_OF], "investigate promptly"
    )
    assert len(candidates) == CANDIDATES
    assert candidates[0].metadata["paragraph_id"] == QUERY_VECTOR_OF
    assert candidates[0].page_content.startswith(QUERY_VECTOR_OF + "\n")


@pytest.mark.vcr
def test_embed_returns_one_1536_dimension_vector_per_text(
    settings: FoundrySettings,
) -> None:
    vectors = embed(settings, ["first text", "second text"])
    assert [len(vector) for vector in vectors] == [1536, 1536]
    assert vectors[0] != vectors[1]


@pytest.mark.vcr
def test_rerank_maps_result_indices_back_to_candidates(
    settings: FoundrySettings,
) -> None:
    candidates = [
        Document(page_content="The office is closed on public holidays."),
        Document(page_content="A provisional credit is due within ten business days."),
        Document(page_content="Receipts are available at the terminal."),
    ]
    ranked = rerank(settings, "When is provisional credit due?", candidates, top_n=2)
    assert len(ranked) == 2
    assert ranked[0] is candidates[1]
    assert ranked[1] in (candidates[0], candidates[2])
