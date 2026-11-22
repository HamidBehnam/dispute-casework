"""The judge-selection run: 25 frozen cases, two repeats, Faithfulness and GEval.

Usage: python evals/judge_spike.py INPUTS ROWS MEASUREMENTS SUMMARY
"""

import os

# DeepEval reads these when it is imported, so they are set before any import
# that reaches it: no telemetry, no .env or key file read, nothing written.
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
os.environ["DEEPEVAL_FILE_SYSTEM"] = "READ_ONLY"
os.environ["DEEPEVAL_DISABLE_LEGACY_KEYFILE"] = "1"
os.environ.pop("CONFIDENT_API_KEY", None)

import argparse
import dataclasses
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepeval.evaluate import evaluate
from deepeval.evaluate.configs import (
    AsyncConfig,
    CacheConfig,
    DisplayConfig,
    ErrorConfig,
)
from deepeval.metrics import BaseMetric, FaithfulnessMetric, GEval
from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase, SingleTurnParams
from judge_model import Judge
from judge_validity import (
    ENVIRONMENT,
    FAITHFULNESS_STEPS,
    GEVAL_STEPS,
    Verdict,
    gate,
    late,
    measurement_outcome,
    measurement_rows,
)
from request_log import RequestLog

from dispute_casework.foundry import FoundrySettings

REPEATS = 2
# The longest case: six requests, each with up to three attempts at the judge's
# 1,740-second timeout, so DeepEval's own case budget never ends a case first.
CASE_TIMEOUT_SECONDS = 6 * 3 * 1740
CITATION_SUPPORT = (
    "Determine whether every statement in the actual output is supported by the "
    "retrieval context and whether every paragraph ID it cites appears in the "
    "retrieval context."
)

JudgeFactory = Callable[[str, int], DeepEvalBaseLLM]


def metrics(judge: DeepEvalBaseLLM) -> list[BaseMetric]:
    return [
        FaithfulnessMetric(model=judge, include_reason=True, async_mode=False),
        GEval(
            name="Citation support",
            criteria=CITATION_SUPPORT,
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.RETRIEVAL_CONTEXT,
            ],
            model=judge,
            async_mode=False,
        ),
    ]


def run(
    cases: list[dict[str, Any]],
    judge_for: JudgeFactory,
    log: RequestLog,
    measurements: Path,
) -> Verdict:
    """Measures every case twice and stops at the first environment failure."""
    outcomes: list[str] = []
    for repeat in range(1, REPEATS + 1):
        for case in cases:
            case_outcomes = measure_case(case, repeat, judge_for, log, measurements)
            outcomes += [o for o in case_outcomes if o != ENVIRONMENT]
            if ENVIRONMENT in case_outcomes:
                return gate(outcomes, stopped=True)
    return gate(outcomes, stopped=False)


def measure_case(
    case: dict[str, Any],
    repeat: int,
    judge_for: JudgeFactory,
    log: RequestLog,
    measurements: Path,
) -> list[str]:
    test_case = LLMTestCase(
        input=case["input"],
        actual_output=case["actual_output"],
        retrieval_context=case["retrieval_context"],
    )
    result = evaluate(
        [test_case],
        metrics(judge_for(case["case_id"], repeat)),
        async_config=AsyncConfig(run_async=False),
        display_config=DisplayConfig(show_indicator=False, print_results=False),
        cache_config=CacheConfig(write_cache=False, use_cache=False),
        error_config=ErrorConfig(ignore_errors=True),
    )
    case_ended_at = time.time()
    rows = [
        row
        for row in log.rows()
        if row["case_id"] == case["case_id"] and row["repeat"] == repeat
    ]
    metrics_data = result.test_results[0].metrics_data or []
    outcomes = []
    with measurements.open("a", encoding="utf-8") as file:
        for name, steps, data in zip(
            ("faithfulness", "geval"),
            (FAITHFULNESS_STEPS, GEVAL_STEPS),
            metrics_data,
            strict=True,
        ):
            own = measurement_rows(rows, steps)
            outcome = measurement_outcome(own, data.error)
            outcomes.append(outcome)
            row = {
                "measurement_id": f"{case['case_id']}-r{repeat}-{name}",
                "case_id": case["case_id"],
                "repeat": repeat,
                "metric": name,
                "score": data.score,
                "threshold": data.threshold,
                "success": data.success,
                "reason": data.reason,
                "error": data.error,
                "request_count": len(own),
                "outcome": outcome,
                "valid": outcome == "valid",
                "case_ended_at": case_ended_at,
            }
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    return outcomes


def late_rows(log: RequestLog, measurements: Path) -> list[dict[str, Any]]:
    """Rows that arrived after DeepEval marked their case timed out."""
    ended = {}
    with measurements.open(encoding="utf-8") as file:
        for line in file:
            row = json.loads(line)
            ended[(row["case_id"], row["repeat"])] = row["case_ended_at"]
    return [
        row
        for row in log.rows()
        if (row["case_id"], row["repeat"]) in ended
        and late(row, ended[(row["case_id"], row["repeat"])])
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path)
    parser.add_argument("rows", type=Path)
    parser.add_argument("measurements", type=Path)
    parser.add_argument("summary", type=Path)
    args = parser.parse_args()
    os.environ["DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE"] = str(CASE_TIMEOUT_SECONDS)
    settings = FoundrySettings()
    log = RequestLog(args.rows)
    cases = json.loads(args.inputs.read_text(encoding="utf-8"))["cases"]
    started_at = time.time()
    verdict = run(
        cases,
        lambda case_id, repeat: Judge(settings, log, case_id, repeat),
        log,
        args.measurements,
    )
    summary = dataclasses.asdict(verdict) | {
        "started_at": started_at,
        "ended_at": time.time(),
        "late_rows": len(late_rows(log, args.measurements)),
    }
    args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
