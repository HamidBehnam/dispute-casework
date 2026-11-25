"""Writes docs/results/0009-judge-selection.json from the judge-selection runs.

The raw readings live under docs/results/0009-judge-selection-raw: per run the
measurement rows, the request rows (a failed answer's headers and body removed)
and the summary the run wrote, with the frozen inputs and the retail prices
read before the first run. Every figure in the results file is computed here
from them.

Usage: python evals/judge_results.py
"""

import hashlib
import json
from dataclasses import dataclass
from importlib.metadata import version
from itertools import pairwise
from pathlib import Path
from statistics import fmean
from typing import Any

from scipy.stats import binomtest

REPOSITORY = Path(__file__).resolve().parents[1]
RAW = REPOSITORY / "docs/results/0009-judge-selection-raw"
RESULTS = REPOSITORY / "docs/results/0009-judge-selection.json"
RUNS = {"first-run": True, "diagnostic": False, "rerun": True}
CONFIDENCE_LEVEL = 0.95

FAITHFULNESS_STEPS = frozenset(
    {"Truths", "Claims", "Verdicts", "FaithfulnessScoreReason"}
)
GEVAL_STEPS = frozenset({"Steps", "ReasonScore"})
STEPS = sorted(FAITHFULNESS_STEPS | GEVAL_STEPS)
JUDGE_FAILURES = frozenset(
    {
        "parse_error",
        "schema_error",
        "blank_content",
        "hidden_re_request",
        "exception_other",
    }
)
TRANSPORT_FAILURES = frozenset({"transport_error", "case_timeout"})
ENVIRONMENT = "environment"

INPUT_METER = "V4 Pro Inp glbl Tokens"
CACHED_METER = "V4 Pro cached glbl Tokens"
OUTPUT_METER = "V4 Pro Outp glbl Tokens"
METERS = (INPUT_METER, CACHED_METER, OUTPUT_METER)
TOKENS_PER_PRICE_UNIT = 1_000
# The Eval design's call counts: 150 golden cases and three repeats for the
# scheduled run, and six arms on top of that for the published comparison.
SCHEDULED_PER_METRIC = 150 * 3
COMPARISON_PER_METRIC = 150 * 6 * 3

METHOD = {
    "confidence_level": CONFIDENCE_LEVEL,
    "interval": "Wilson interval, scipy.stats.binomtest(k, n).proportion_ci("
    "method='wilson'), rounded to 4 places",
    "slots": "a slot is one case, repeat and metric; its final outcome is its "
    "re-measurement's when it has one, otherwise its first",
    "gate": "over the final outcomes with environment outcomes removed: s judge "
    "failures, t transport failures, k = n - s - t; fail when s >= 2, otherwise "
    "stopped by environment when any environment outcome, pass when t = 0, "
    "otherwise undecided by transport",
    "counted": "false marks the uncounted diagnostic run; its verdict is not a gate "
    "result",
    "transport": "attempts from the trace events of each request; null for a run "
    "recorded without them",
    "cost": "the mean cost of each step's requests with usage, priced with the "
    "retail meters; a measurement costs the sum of its metric's step means; the "
    "scheduled run is 450 measurements per metric and the comparison 2,700; the "
    "no-cache bound prices every prompt token at the input meter; USD rounded to "
    "6 places",
    "measured_with": {
        "first-run": "DeepSeek-V4-Pro on chat completions, 1740 s timeout, the "
        "SDK's default of 2 retries; harness at bb3f134, the last commit before "
        "the run started",
        "diagnostic": "DeepSeek-V4-Pro on chat completions, 1740 s timeout, the "
        "SDK's default of 2 retries; harness at f7ddcc9, the last commit before "
        "the run started",
        "rerun": "DeepSeek-V4-Pro on chat completions, 1740 s timeout, 5 retries; "
        "harness at 960b6bf, the last commit before the run started",
        "committed_judge": "360 s timeout and 8 retries, decided after these runs "
        "and measured by none of them",
    },
    "percent": "round(100 * k / n), beside the count",
    "versions": {"scipy": version("scipy")},
}


@dataclass(frozen=True)
class Verdict:
    n: int
    k: int
    s: int
    t: int
    verdict: str


def gate(outcomes: list[str], *, stopped: bool) -> Verdict:
    """The gate over completed measurements; a stopped run counts only those."""
    s = sum(outcome in JUDGE_FAILURES for outcome in outcomes)
    t = sum(outcome in TRANSPORT_FAILURES for outcome in outcomes)
    if s >= 2:
        verdict = "fail"
    elif stopped:
        verdict = "stopped by environment, undecided"
    elif t == 0:
        verdict = "pass"
    else:
        verdict = "undecided by transport"
    return Verdict(n=len(outcomes), k=len(outcomes) - s - t, s=s, t=t, verdict=verdict)


def lines(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file]


def count(k: int, n: int) -> dict[str, Any]:
    interval = binomtest(k, n).proportion_ci(
        confidence_level=CONFIDENCE_LEVEL, method="wilson"
    )
    return {
        "k": k,
        "n": n,
        "percent": round(100 * k / n),
        "interval": [round(interval.low, 4), round(interval.high, 4)],
    }


def slots(measurements: list[dict[str, Any]]) -> list[dict[str, str]]:
    first: dict[str, str] = {}
    final: dict[str, str] = {}
    for row in measurements:
        slot = f"{row['case_id']}-r{row['repeat']}-{row['metric']}"
        if row.get("remeasured", False):
            final[slot] = row["outcome"]
        else:
            first[slot] = row["outcome"]
    return [
        {"slot": slot, "first": outcome, "final": final.get(slot, outcome)}
        for slot, outcome in first.items()
    ]


def measurement_counts(finals: list[str]) -> dict[str, Any]:
    environment = finals.count(ENVIRONMENT)
    completed = [outcome for outcome in finals if outcome != ENVIRONMENT]
    verdict = gate(completed, stopped=environment > 0)
    return {
        "valid": count(verdict.k, verdict.n),
        "s": verdict.s,
        "t": verdict.t,
        "environment": environment,
        "verdict": verdict.verdict,
    }


def request_counts(requests: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "requests": len(requests),
        "valid": sum(row["outcome"] == "valid" for row in requests),
        "strict_json": sum(row["strict_json"] for row in requests),
        "repaired": sum(row["repaired"] for row in requests),
    }


def transport(requests: list[dict[str, Any]]) -> dict[str, Any] | None:
    attempts = [attempt for row in requests for attempt in row["attempts"]]
    if not all("events" in attempt for attempt in attempts):
        return None
    new = [attempt for attempt in attempts if attempt["new_connection"]]
    handshake_failures = sum(
        event["name"] == "connection.start_tls.failed"
        for attempt in attempts
        for event in attempt["events"]
    )
    after_429 = [
        attempt
        for row in requests
        for previous, attempt in pairwise(row["attempts"])
        if previous.get("status") == 429
    ]
    new_after_429 = sum(attempt["new_connection"] for attempt in after_429)
    return {
        "attempts": len(attempts),
        "new_connections": len(new),
        "reused_connections": len(attempts) - len(new),
        "handshake_failures_of_new_connections": count(handshake_failures, len(new)),
        "attempts_after_a_429": {
            "new_connection": new_after_429,
            "reused_connection": len(after_429) - new_after_429,
        },
    }


def price(prices: dict[str, Any], meter: str) -> float:
    [item] = [
        item
        for item in prices["Items"]
        if item["meterName"] == meter and item["unitOfMeasure"] == "1K"
    ]
    return float(item["retailPrice"]) / TOKENS_PER_PRICE_UNIT


def cost(requests: list[dict[str, Any]], prices: dict[str, Any]) -> dict[str, Any]:
    rates = {meter: price(prices, meter) for meter in METERS}
    priced = [row for row in requests if row["usage"]]

    def usd(row: dict[str, Any], *, cache: bool, output: bool = True) -> float:
        usage = row["usage"]
        cached = usage["prompt_tokens_details"]["cached_tokens"] if cache else 0
        prompt_usd = (usage["prompt_tokens"] - cached) * rates[INPUT_METER]
        prompt_usd += cached * rates[CACHED_METER]
        if not output:
            return float(prompt_usd)
        return float(prompt_usd + usage["completion_tokens"] * rates[OUTPUT_METER])

    def step_rows(step: str) -> list[dict[str, Any]]:
        return [row for row in priced if row["step"] == step]

    def mean_tokens(step: str) -> dict[str, Any]:
        usages = [row["usage"] for row in step_rows(step)]
        return {
            "requests_with_usage": len(usages),
            "prompt": round(fmean(u["prompt_tokens"] for u in usages), 2),
            "cached": round(
                fmean(u["prompt_tokens_details"]["cached_tokens"] for u in usages), 2
            ),
            "completion": round(fmean(u["completion_tokens"] for u in usages), 2),
        }

    def estimate(cache: bool) -> dict[str, Any]:
        mean = {
            step: fmean(usd(row, cache=cache) for row in step_rows(step))
            for step in STEPS
        }
        faithfulness = sum(mean[step] for step in sorted(FAITHFULNESS_STEPS))
        geval = sum(mean[step] for step in sorted(GEVAL_STEPS))
        truths_input = fmean(
            usd(row, cache=cache, output=False) for row in step_rows("Truths")
        )
        return {
            "steps": {step: round(usd_mean, 6) for step, usd_mean in mean.items()},
            "measurement_faithfulness": round(faithfulness, 6),
            "measurement_geval": round(geval, 6),
            "scheduled_run": round(SCHEDULED_PER_METRIC * (faithfulness + geval), 6),
            "comparison_faithfulness": round(COMPARISON_PER_METRIC * faithfulness, 6),
            "comparison_geval": round(COMPARISON_PER_METRIC * geval, 6),
            "comparison_truths_input": round(COMPARISON_PER_METRIC * truths_input, 6),
        }

    return {
        "requests": len(requests),
        "requests_with_usage": len(priced),
        "usd_per_token": rates,
        "tokens_mean": {step: mean_tokens(step) for step in STEPS},
        "estimate_usd": estimate(cache=True),
        "estimate_usd_without_cache": estimate(cache=False),
    }


def results(raw: Path) -> dict[str, Any]:
    prices = json.loads((raw / "prices-deepseek-eastus2.json").read_text())
    runs = {}
    for name, counted in RUNS.items():
        requests = lines(raw / name / "requests.jsonl")
        run_slots = slots(lines(raw / name / "measurements.jsonl"))
        runs[name] = {
            "counted": counted,
            "measurements": measurement_counts([slot["final"] for slot in run_slots]),
            "slots": run_slots,
            "requests": request_counts(requests),
            "transport": transport(requests),
            "cost": cost(requests, prices),
        }
    return {
        "method": METHOD,
        "inputs_sha256": hashlib.sha256((raw / "inputs.json").read_bytes()).hexdigest(),
        "runs": runs,
    }


def main() -> None:
    RESULTS.write_text(json.dumps(results(RAW), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
