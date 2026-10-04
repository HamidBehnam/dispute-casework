"""Exit test of the retrieval store on the hand-written query set.

Thresholds were fixed before the first run. The gates cover the exact-term and
paraphrased strata; the citation stratum is measured and reported only, because
an exact citation is served by a lookup, not by retrieval. Both tests replay the
one recorded run.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.documents import Document
from langchain_postgres import PGVectorStore
from langchain_postgres.v2.hybrid_search_config import reciprocal_rank_fusion

from dispute_casework import retrieval
from dispute_casework.foundry import FoundrySettings
from dispute_casework.retrieval import CANDIDATES, embed, rerank, retrieve

RERANK_TOP_N = 5
# Recording stays under the rerank deployment's tokens-per-minute limit.
SECONDS_BEFORE_RERANK_WHEN_RECORDING = 30
GATE_D_NOT_MET = (
    "ADR 0004: gate d is not met in the recorded run. On the gate strata recall@5 "
    "after rerank is 22/26, under the 85% threshold (23/26) and under the fused "
    "top 5 (23/26)."
)


@dataclass(frozen=True)
class Query:
    stratum: str
    text: str
    expected: list[str]
    keywords: str


@dataclass(frozen=True)
class Outcome:
    query: Query
    ranked: dict[str, list[str]]
    keyword_leg_rows: dict[str, int]


QUERIES = [
    Query(**query)
    for query in json.loads((Path(__file__).parent / "queries.json").read_text())
]


def paragraph_ids(documents: list[Document]) -> list[str]:
    return [document.metadata["paragraph_id"] for document in documents]


def measure(
    store: PGVectorStore, settings: FoundrySettings, queries: list[Query], pause: float
) -> list[Outcome]:
    """Runs every arm for every query; the paragraph IDs come back best first."""
    vectors = embed(settings, [query.text for query in queries])
    outcomes = []
    with patch.object(
        retrieval, "reciprocal_rank_fusion", wraps=reciprocal_rank_fusion
    ) as fusion:
        for query, vector in zip(queries, vectors, strict=True):
            dense = store.similarity_search_by_vector(
                vector, k=CANDIDATES, hybrid_search_config=None
            )
            hybrid_raw = retrieve(store, vector, query.text)
            raw_rows = len(fusion.call_args.args[1])
            hybrid_keyword = retrieve(store, vector, query.keywords)
            keyword_rows = len(fusion.call_args.args[1])
            time.sleep(pause)
            dense_rerank = rerank(settings, query.text, dense, RERANK_TOP_N)
            time.sleep(pause)
            hybrid_rerank = rerank(settings, query.text, hybrid_keyword, RERANK_TOP_N)
            outcomes.append(
                Outcome(
                    query=query,
                    ranked={
                        "dense": paragraph_ids(dense),
                        "hybrid_raw": paragraph_ids(hybrid_raw),
                        "hybrid_keyword": paragraph_ids(hybrid_keyword),
                        "dense_rerank": paragraph_ids(dense_rerank),
                        "hybrid_keyword_rerank": paragraph_ids(hybrid_rerank),
                    },
                    keyword_leg_rows={
                        "hybrid_raw": raw_rows,
                        "hybrid_keyword": keyword_rows,
                    },
                )
            )
    return outcomes


def recall(outcomes: list[Outcome], arm: str, k: int) -> float:
    """Share of expected paragraph IDs found in the arm's first k results."""
    expected = sum(len(outcome.query.expected) for outcome in outcomes)
    found = sum(
        len(set(outcome.query.expected) & set(outcome.ranked[arm][:k]))
        for outcome in outcomes
    )
    return found / expected


def gate_strata(outcomes: list[Outcome]) -> list[Outcome]:
    return [outcome for outcome in outcomes if outcome.query.stratum != "citation"]


def keyword_leg_rate(outcomes: list[Outcome], arm: str) -> float:
    """Share of queries whose keyword leg returned at least one row."""
    return sum(outcome.keyword_leg_rows[arm] > 0 for outcome in outcomes) / len(
        outcomes
    )


@pytest.mark.vcr
def test_exit_gates(
    store: PGVectorStore, settings: FoundrySettings, record_mode: str
) -> None:
    pause = 0 if record_mode == "none" else SECONDS_BEFORE_RERANK_WHEN_RECORDING
    gated = gate_strata(measure(store, settings, QUERIES, pause))

    assert keyword_leg_rate(gated, "hybrid_keyword") >= 0.90
    assert recall(gated, "hybrid_keyword", 20) >= 0.90


# Never records: when the cassette is recorded again, test_exit_gates writes it
# and this test replays what was written.
@pytest.mark.vcr(record_mode="none")
@pytest.mark.default_cassette("test_exit_gates")
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=GATE_D_NOT_MET)
def test_recall_at_5_after_rerank_gate(
    store: PGVectorStore, settings: FoundrySettings
) -> None:
    gated = gate_strata(measure(store, settings, QUERIES, pause=0))

    assert recall(gated, "hybrid_keyword_rerank", 5) >= 0.85
    assert recall(gated, "hybrid_keyword_rerank", 5) >= recall(
        gated, "hybrid_keyword", 5
    )
