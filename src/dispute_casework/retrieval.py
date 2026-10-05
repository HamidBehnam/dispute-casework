"""Retrieval seams (embed, retrieve, rerank) and the chunk store they work on."""

from langchain_core.documents import Document
from langchain_postgres import PGEngine, PGVectorStore
from langchain_postgres.v2.hybrid_search_config import (
    HybridSearchConfig,
    reciprocal_rank_fusion,
)
from langchain_postgres.v2.indexes import DistanceStrategy

from dispute_casework.foundry import FoundrySettings, embeddings, reranker

TABLE = "chunks"
ID_COLUMN = "chunk_id"
METADATA_COLUMNS = [
    "paragraph_id",
    "part",
    "section",
    "source",
    "legal_status",
    "snapshot",
]
TSV_COLUMN = "content_tsv"
TSV_LANG = "pg_catalog.english"
LEG_TOP_K = 40
CANDIDATES = 20
RRF_K = 60
RERANK_DEPLOYMENT = "Cohere-rerank-v4.0-fast"
RERANK_MAX_TOKENS_PER_DOC = 512


def hybrid_config(keywords: str) -> HybridSearchConfig:
    """A new config for one search.

    langchain-postgres 0.0.18 writes the query and fetch_top_k into the config
    it is given, so a shared instance would carry one search's keyword query
    into the next (langchain-postgres issue 337). An empty tsv_column makes the
    vendor recompute to_tsvector per row instead of reading the stored column.
    """
    return HybridSearchConfig(
        tsv_column=TSV_COLUMN,
        tsv_lang=TSV_LANG,
        fts_query=keywords,
        fusion_function=reciprocal_rank_fusion,
        fusion_function_parameters={"rrf_k": RRF_K},
        primary_top_k=LEG_TOP_K,
        secondary_top_k=LEG_TOP_K,
    )


def open_store(engine: PGEngine, settings: FoundrySettings) -> PGVectorStore:
    # The store-level config tells inserts which tsvector column to fill and in
    # which language; every search passes its own.
    return PGVectorStore.create_sync(
        engine,
        embeddings(settings),
        TABLE,
        id_column=ID_COLUMN,
        metadata_columns=METADATA_COLUMNS,
        distance_strategy=DistanceStrategy.COSINE_DISTANCE,
        hybrid_search_config=hybrid_config(""),
    )


def embed(settings: FoundrySettings, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = embeddings(settings).embed_documents(texts)
    return vectors


def retrieve(
    store: PGVectorStore, embedding: list[float], keywords: str
) -> list[Document]:
    if not keywords.strip():
        # With an empty fts_query langchain-postgres 0.0.18 skips the keyword leg
        # and the fusion, and returns the dense leg's rows instead of k.
        raise ValueError("retrieve needs a keyword string for the keyword leg")
    return store.similarity_search_by_vector(
        embedding, k=CANDIDATES, hybrid_search_config=hybrid_config(keywords)
    )


def rerank(
    settings: FoundrySettings, query: str, candidates: list[Document], top_n: int
) -> list[Document]:
    response = reranker(settings).rerank(
        model=RERANK_DEPLOYMENT,
        query=query,
        documents=[candidate.page_content for candidate in candidates],
        top_n=top_n,
        max_tokens_per_doc=RERANK_MAX_TOKENS_PER_DOC,
    )
    return [candidates[result.index] for result in response.results]
