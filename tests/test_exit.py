"""Exit test of the retrieval store on the hand-written query set.

Thresholds were fixed before the run. The gates cover the exact-term and
paraphrased strata; the citation stratum is measured and reported only, because
an exact citation is served by a lookup, not by retrieval. Every test that needs
the run replays the one recorded run.
"""

import json
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from statistics import fmean
from typing import Any
from unittest.mock import patch

import ir_measures
import pytest
from deepeval import evaluate
from deepeval.evaluate.configs import AsyncConfig, CacheConfig, DisplayConfig
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from ir_measures import NumRel, NumRelRet, Qrel, R, ScoredDoc
from langchain_core.documents import Document
from langchain_postgres import PGVectorStore
from langchain_postgres.v2.hybrid_search_config import reciprocal_rank_fusion
from scipy.stats import binomtest

from dispute_casework import retrieval
from dispute_casework.foundry import FoundrySettings
from dispute_casework.retrieval import CANDIDATES, embed, rerank, retrieve

RERANK_TOP_N = 5
CONFIDENCE_LEVEL = 0.95
RESULTS = Path(__file__).parents[1] / "docs/results/0004-exit-run.json"
CUTOFFS = {
    "dense": [CANDIDATES, RERANK_TOP_N],
    "hybrid_raw": [CANDIDATES, RERANK_TOP_N],
    "hybrid_keyword": [CANDIDATES, RERANK_TOP_N],
    "dense_rerank": [RERANK_TOP_N],
    "hybrid_keyword_rerank": [RERANK_TOP_N],
}
RERANK_OF = {"dense_rerank": "dense", "hybrid_keyword_rerank": "hybrid_keyword"}
GATE_D_NOT_MET = (
    "ADR 0004: gate d is not met in the recorded run. On the gate strata the mean "
    "recall@5 after rerank is 0.84 (22/26 expected IDs), under the 0.85 threshold "
    "and under the fused top 5 at 0.88 (23/26)."
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
    dense_leg: list[str]


QUERIES = [
    Query(**query)
    for query in json.loads(
        (Path(__file__).parent / "queries.json").read_text(encoding="utf-8")
    )
]


class RecallAtK(BaseMetric):
    """Share of a query's expected paragraph IDs among its first k results."""

    def __init__(self, k: int) -> None:
        self.k = k
        # evaluate() refuses a list of metrics in which none has a threshold.
        self.threshold = 1.0

    @property
    def __name__(self) -> str:
        return f"citation-ID recall@{self.k}"

    def measure(self, test_case: LLMTestCase, *args: Any, **kwargs: Any) -> float:
        qrels = [Qrel("query", paragraph_id, 1) for paragraph_id in test_case.context]
        # trec_eval ranks by score and breaks ties by document ID, so the
        # scores fall with the position.
        run = [
            ScoredDoc("query", paragraph_id, -position)
            for position, paragraph_id in enumerate(test_case.retrieval_context)
        ]
        self.score = ir_measures.calc_aggregate([R @ self.k], qrels, run)[R @ self.k]
        return self.score

    async def a_measure(
        self, test_case: LLMTestCase, *args: Any, **kwargs: Any
    ) -> float:
        return self.measure(test_case)


def paragraph_ids(documents: list[Document]) -> list[str]:
    return [document.metadata["paragraph_id"] for document in documents]


def measure(
    store: PGVectorStore, settings: FoundrySettings, queries: list[Query]
) -> list[Outcome]:
    """Runs every arm for every query; the paragraph IDs come back best first."""
    vectors = embed(settings, [query.text for query in queries])
    outcomes = []
    with patch.object(
        retrieval, "reciprocal_rank_fusion", wraps=reciprocal_rank_fusion
    ) as fusion:
        for query, vector in zip(queries, vectors, strict=True):
            # None turns the hybrid search off; left out, the vendor falls back to
            # the store's own config and returns the dense leg's 40 rows.
            dense = store.similarity_search_by_vector(
                vector, k=CANDIDATES, hybrid_search_config=None
            )
            hybrid_raw = retrieve(store, vector, query.text)
            raw_rows = len(fusion.call_args.args[1])
            hybrid_keyword = retrieve(store, vector, query.keywords)
            dense_leg, keyword_leg = fusion.call_args.args[:2]
            dense_rerank = rerank(settings, query.text, dense, RERANK_TOP_N)
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
                        "hybrid_keyword": len(keyword_leg),
                    },
                    dense_leg=[row["paragraph_id"] for row in dense_leg],
                )
            )
    return outcomes


def scores(outcomes: list[Outcome], arm: str, k: int) -> list[float]:
    """Recall@k of each query, in the order of the outcomes."""
    evaluation = evaluate(
        [
            LLMTestCase(
                input=outcome.query.text,
                context=outcome.query.expected,
                retrieval_context=outcome.ranked[arm],
            )
            for outcome in outcomes
        ],
        [RecallAtK(k)],
        async_config=AsyncConfig(run_async=False),
        display_config=DisplayConfig(
            show_indicator=False, print_results=False, inspect_after_run=False
        ),
        cache_config=CacheConfig(write_cache=False),
    )
    return [result.metrics_data[0].score for result in evaluation.test_results]


def recall(outcomes: list[Outcome], arm: str, k: int) -> float:
    return fmean(scores(outcomes, arm, k))


def gate_strata(outcomes: list[Outcome]) -> list[Outcome]:
    return [outcome for outcome in outcomes if outcome.query.stratum != "citation"]


def rank(paragraph_id: str, ranked: list[str]) -> int | None:
    return ranked.index(paragraph_id) + 1 if paragraph_id in ranked else None


def cell(
    outcomes: list[Outcome], arm: str, k: int, recalls: list[float]
) -> dict[str, Any]:
    """One arm at one cut-off: the gated mean, the pooled count and the interval."""
    pooled = ir_measures.calc_aggregate(
        [NumRelRet, NumRel],
        [
            Qrel(str(number), paragraph_id, 1)
            for number, outcome in enumerate(outcomes)
            for paragraph_id in outcome.query.expected
        ],
        [
            ScoredDoc(str(number), paragraph_id, -position)
            for number, outcome in enumerate(outcomes)
            for position, paragraph_id in enumerate(outcome.ranked[arm][:k])
        ],
    )
    complete = recalls.count(1.0)
    interval = binomtest(complete, len(outcomes)).proportion_ci(
        confidence_level=CONFIDENCE_LEVEL, method="wilson"
    )
    return {
        "mean_recall": round(fmean(recalls), 4),
        "found": int(pooled[NumRelRet]),
        "expected": int(pooled[NumRel]),
        "queries_with_every_expected_id": complete,
        "share_of_queries_with_every_expected_id_interval": [
            round(interval.low, 4),
            round(interval.high, 4),
        ],
    }


def found_by(outcomes: list[Outcome], arm: str, missed_by: str) -> list[str]:
    """Expected IDs among one arm's twenty candidates and not among the other's."""
    return [
        paragraph_id
        for outcome in outcomes
        for paragraph_id in outcome.query.expected
        if paragraph_id in outcome.ranked[arm]
        and paragraph_id not in outcome.ranked[missed_by]
    ]


def rerank_against_first_five(
    after: list[float], before: list[float]
) -> dict[str, Any]:
    """Queries whose recall@5 is higher, and lower, after rerank than before it."""
    pairs = list(zip(after, before, strict=True))
    higher = sum(reranked > first_five for reranked, first_five in pairs)
    lower = sum(reranked < first_five for reranked, first_five in pairs)
    return {
        "higher": higher,
        "lower": lower,
        "p_value": round(binomtest(higher, higher + lower, 0.5).pvalue, 4)
        if higher + lower
        else None,
    }


def stratum_results(outcomes: list[Outcome]) -> dict[str, Any]:
    per_query = {
        (arm, k): scores(outcomes, arm, k)
        for arm, cutoffs in CUTOFFS.items()
        for k in cutoffs
    }
    arms: dict[str, Any] = {
        arm: {
            f"recall_at_{k}": cell(outcomes, arm, k, per_query[arm, k]) for k in cutoffs
        }
        for arm, cutoffs in CUTOFFS.items()
    }
    for arm in ("hybrid_raw", "hybrid_keyword"):
        arms[arm] |= {
            "keyword_leg_non_empty": sum(
                outcome.keyword_leg_rows[arm] > 0 for outcome in outcomes
            ),
            "rescued": found_by(outcomes, arm, missed_by="dense"),
            "lost": found_by(outcomes, "dense", missed_by=arm),
        }
    for reranked, before in RERANK_OF.items():
        arms[reranked]["against_first_five_before_rerank"] = rerank_against_first_five(
            per_query[reranked, RERANK_TOP_N], per_query[before, RERANK_TOP_N]
        )
    return {
        "queries": len(outcomes),
        "expected_ids": sum(len(outcome.query.expected) for outcome in outcomes),
        "arms": arms,
    }


def results(outcomes: list[Outcome]) -> dict[str, Any]:
    """Every published figure of the run, as the results file holds it."""
    strata = {
        stratum: [outcome for outcome in outcomes if outcome.query.stratum == stratum]
        for stratum in ("citation", "exact-term", "paraphrased")
    } | {"gate strata": gate_strata(outcomes)}
    return {
        "method": {
            "recall": "mean over queries of R@k from ir-measures (trec_eval recall_k); "
            "found and expected are NumRelRet and NumRel on the run cut to k",
            "interval": "Wilson interval for the share of queries with every expected "
            "ID found, scipy.stats.binomtest(...).proportion_ci(method='wilson')",
            "confidence_level": CONFIDENCE_LEVEL,
            "rerank_test": "queries whose recall@5 is higher after rerank against "
            "queries where it is lower, scipy.stats.binomtest(higher, higher + lower, "
            "0.5); null when no query differs",
            "versions": {
                package: version(package)
                for package in (
                    "deepeval",
                    "ir-measures",
                    "pytrec-eval-terrier",
                    "scipy",
                    "langchain-postgres",
                )
            },
        },
        "strata": {
            stratum: stratum_results(members) for stratum, members in strata.items()
        },
        "queries": [
            {
                "number": number,
                "stratum": outcome.query.stratum,
                "keyword_leg_rows": {
                    "raw_question": outcome.keyword_leg_rows["hybrid_raw"],
                    "keyword_string": outcome.keyword_leg_rows["hybrid_keyword"],
                },
                "ranks": {
                    paragraph_id: {
                        arm: rank(paragraph_id, ranked)
                        for arm, ranked in outcome.ranked.items()
                    }
                    for paragraph_id in outcome.query.expected
                },
            }
            for number, outcome in enumerate(outcomes, start=1)
        ],
        "missed_after_rerank": [
            {
                "query": number,
                "paragraph_id": paragraph_id,
                "rank_in_fused_twenty": rank(
                    paragraph_id, outcome.ranked["hybrid_keyword"]
                ),
                "rank_in_dense_top_40": rank(paragraph_id, outcome.dense_leg),
            }
            for number, outcome in enumerate(outcomes, start=1)
            for paragraph_id in outcome.query.expected
            if paragraph_id not in outcome.ranked["hybrid_keyword_rerank"]
        ],
    }


def test_scores_come_back_one_per_query_in_order_as_the_metric_computed_them() -> None:
    ranked = [["a", "b", "c"], ["c", "a", "d", "e", "f", "b"], ["x"], []]
    outcomes = [
        Outcome(
            Query("exact-term", f"query {number}", ["a", "b"], ""), {"arm": ids}, {}, []
        )
        for number, ids in enumerate(ranked)
    ]
    assert scores(outcomes, "arm", 5) == [1.0, 0.5, 0.0, 0.0]
    assert scores(outcomes, "arm", 20) == [1.0, 1.0, 0.0, 0.0]
    assert recall(outcomes, "arm", 5) == 0.375


@pytest.mark.vcr
def test_exit_gates(store: PGVectorStore, settings: FoundrySettings) -> None:
    gated = gate_strata(measure(store, settings, QUERIES))

    assert (
        sum(outcome.keyword_leg_rows["hybrid_keyword"] > 0 for outcome in gated)
        / len(gated)
        >= 0.90
    )
    assert recall(gated, "hybrid_keyword", 20) >= 0.90


# Never records: when the cassette is recorded again, test_exit_gates writes it
# and the tests below replay what was written.
@pytest.mark.vcr(record_mode="none")
@pytest.mark.default_cassette("test_exit_gates")
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=GATE_D_NOT_MET)
def test_recall_at_5_after_rerank_gate(
    store: PGVectorStore, settings: FoundrySettings
) -> None:
    gated = gate_strata(measure(store, settings, QUERIES))
    after_rerank = recall(gated, "hybrid_keyword_rerank", 5)

    assert after_rerank >= 0.85
    assert after_rerank >= recall(gated, "hybrid_keyword", 5)


@pytest.mark.vcr(record_mode="none")
@pytest.mark.default_cassette("test_exit_gates")
def test_results_file_is_current(
    store: PGVectorStore, settings: FoundrySettings, tmp_path: Path
) -> None:
    generated = (
        json.dumps(results(measure(store, settings, QUERIES)), indent=2, sort_keys=True)
        + "\n"
    )
    if generated != RESULTS.read_text(encoding="utf-8"):
        regenerated = tmp_path / RESULTS.name
        regenerated.write_text(generated, encoding="utf-8")
        pytest.fail(
            f"{RESULTS} is not what the recorded run produces; "
            f"the regenerated file is {regenerated}"
        )
