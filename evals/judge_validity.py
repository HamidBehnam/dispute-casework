"""Schema validity of judge requests and measurements, and the pre-registered gate."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import openai
from azure.core.exceptions import ClientAuthenticationError
from deepeval.errors import DeepEvalError
from pydantic import ValidationError

# The SDK retries these itself, so an answer still carrying one is transport.
RETRIED_STATUSES = frozenset({408, 409, 429})
FAITHFULNESS_STEPS = frozenset(
    {"Truths", "Claims", "Verdicts", "FaithfulnessScoreReason"}
)
GEVAL_STEPS = frozenset({"Steps", "ReasonScore"})
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
# DeepEval's messages for a case that outlived its time budget
# (deepeval/evaluate/execute/_common.py _timeout_msg and e2e.py).
CASE_TIMEOUT_MARKERS = ("Timed out after", "Skipped due to case timeout.")


def failure(error: BaseException) -> str:
    if isinstance(error, ClientAuthenticationError):
        return ENVIRONMENT
    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        if status in RETRIED_STATUSES or status >= 500:
            return "transport_error"
        return ENVIRONMENT
    if isinstance(error, openai.APIConnectionError):
        return "transport_error"
    return "exception_other"


def request_outcome(
    has_schema: bool, content: str | None, error: BaseException | None
) -> str:
    if content is None:
        return failure(error) if error else "exception_other"
    if not has_schema:
        return "no_schema"
    if not content.strip():
        return "blank_content"
    if isinstance(error, DeepEvalError):
        return "parse_error"
    if isinstance(error, ValidationError):
        return "schema_error"
    if error:
        return "exception_other"
    return "valid"


def measurement_rows(
    rows: Iterable[dict[str, Any]], steps: frozenset[str]
) -> list[dict[str, Any]]:
    """A metric's rows: its own steps, and schemaless rows repeating their prompts."""
    rows = list(rows)
    own = [row for row in rows if row["step"] in steps]
    prompts = {row["prompt_sha256"] for row in own}
    repeats = [
        row for row in rows if row["step"] is None and row["prompt_sha256"] in prompts
    ]
    return sorted(own + repeats, key=lambda row: row["started_at"])


def measurement_outcome(rows: list[dict[str, Any]], error: str | None) -> str:
    outcomes: list[str] = [row["outcome"] for row in rows]
    if ENVIRONMENT in outcomes:
        return ENVIRONMENT
    prompts = [row["prompt_sha256"] for row in rows]
    if len(prompts) != len(set(prompts)):
        return "hidden_re_request"
    judged = [outcome for outcome in outcomes if outcome in JUDGE_FAILURES]
    if judged:
        return judged[0]
    if error and any(marker in error for marker in CASE_TIMEOUT_MARKERS):
        return "case_timeout"
    if "transport_error" in outcomes:
        return "transport_error"
    if error:
        return "exception_other"
    return "valid"


def late(row: dict[str, Any], case_ended_at: float) -> bool:
    return bool(row["ended_at"] > case_ended_at)


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
