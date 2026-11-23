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
from judge_model import JUDGE_MAX_RETRIES, JUDGE_TIMEOUT_SECONDS, Judge
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
METRICS = ("faithfulness", "geval")
STEPS = {"faithfulness": FAITHFULNESS_STEPS, "geval": GEVAL_STEPS}
# The longest case: six requests, each with all its attempts at the judge's
# timeout, so DeepEval's own case budget never ends a case first. The waits
# between attempts come on top.
CASE_TIMEOUT_SECONDS = 6 * (1 + JUDGE_MAX_RETRIES) * JUDGE_TIMEOUT_SECONDS
CITATION_SUPPORT = (
    "Determine whether every statement in the actual output is supported by the "
    "retrieval context and whether every paragraph ID it cites appears in the "
    "retrieval context."
)

JudgeFactory = Callable[[str, int, bool], DeepEvalBaseLLM]


def metric(name: str, judge: DeepEvalBaseLLM) -> BaseMetric:
    if name == "faithfulness":
        return FaithfulnessMetric(model=judge, include_reason=True, async_mode=False)
    return GEval(
        name="Citation support",
        criteria=CITATION_SUPPORT,
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.RETRIEVAL_CONTEXT,
        ],
        model=judge,
        async_mode=False,
    )


def run(
    cases: list[dict[str, Any]],
    judge_for: JudgeFactory,
    log: RequestLog,
    measurements: Path,
) -> tuple[Verdict, list[dict[str, Any]]]:
    """Measures every slot twice over the cases and re-measures a transport cut once.

    A slot whose first measurement ends transport_error is measured once more,
    alone, with a fresh judge; its final outcome is the re-measurement's. An
    environment failure stops the run before any further request is spent.
    """
    slots: list[dict[str, Any]] = []
    for repeat in range(1, REPEATS + 1):
        for case in cases:
            first = measure_case(case, repeat, METRICS, judge_for, log, measurements)
            final = dict(first)
            remeasured = []
            for name in METRICS:
                if first[name] != "transport_error" or ENVIRONMENT in final.values():
                    continue
                final |= measure_case(
                    case, repeat, (name,), judge_for, log, measurements, remeasured=True
                )
                remeasured.append(name)
            slots += [
                {
                    "slot": f"{case['case_id']}-r{repeat}-{name}",
                    "first": first[name],
                    "final": final[name],
                    "remeasured": name in remeasured,
                }
                for name in METRICS
            ]
            if ENVIRONMENT in final.values():
                completed = [s["final"] for s in slots if s["final"] != ENVIRONMENT]
                return gate(completed, stopped=True), slots
    return gate([s["final"] for s in slots], stopped=False), slots


def measure_case(
    case: dict[str, Any],
    repeat: int,
    names: tuple[str, ...],
    judge_for: JudgeFactory,
    log: RequestLog,
    measurements: Path,
    *,
    remeasured: bool = False,
) -> dict[str, str]:
    test_case = LLMTestCase(
        input=case["input"],
        actual_output=case["actual_output"],
        retrieval_context=case["retrieval_context"],
    )
    judge = judge_for(case["case_id"], repeat, remeasured)
    result = evaluate(
        [test_case],
        [metric(name, judge) for name in names],
        async_config=AsyncConfig(run_async=False),
        display_config=DisplayConfig(show_indicator=False, print_results=False),
        cache_config=CacheConfig(write_cache=False, use_cache=False),
        error_config=ErrorConfig(ignore_errors=True),
    )
    case_ended_at = time.time()
    rows = [
        row
        for row in log.rows()
        if row["case_id"] == case["case_id"]
        and row["repeat"] == repeat
        and row["remeasured"] == remeasured
    ]
    metrics_data = result.test_results[0].metrics_data or []
    outcomes = {}
    suffix = "-remeasured" if remeasured else ""
    with measurements.open("a", encoding="utf-8") as file:
        for name, data in zip(names, metrics_data, strict=True):
            own = measurement_rows(rows, STEPS[name])
            outcome = measurement_outcome(own, data.error)
            outcomes[name] = outcome
            row = {
                "measurement_id": f"{case['case_id']}-r{repeat}-{name}{suffix}",
                "case_id": case["case_id"],
                "repeat": repeat,
                "metric": name,
                "remeasured": remeasured,
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
    """Rows that arrived after DeepEval marked their measurement's case timed out."""
    rows = log.rows()
    found = []
    with measurements.open(encoding="utf-8") as file:
        for line in file:
            measurement = json.loads(line)
            same = [
                row
                for row in rows
                if row["case_id"] == measurement["case_id"]
                and row["repeat"] == measurement["repeat"]
                and row["remeasured"] == measurement["remeasured"]
            ]
            found += [
                row
                for row in measurement_rows(same, STEPS[measurement["metric"]])
                if late(row, measurement["case_ended_at"])
            ]
    return found


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
    verdict, slots = run(
        cases,
        lambda case_id, repeat, remeasured: Judge(
            settings, log, case_id, repeat, remeasured=remeasured
        ),
        log,
        args.measurements,
    )
    summary = dataclasses.asdict(verdict) | {
        "started_at": started_at,
        "ended_at": time.time(),
        "late_rows": len(late_rows(log, args.measurements)),
        "remeasured_slots": [s["slot"] for s in slots if s["remeasured"]],
        "slots": slots,
    }
    args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
