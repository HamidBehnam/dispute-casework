import json
from pathlib import Path

import pytest
from judge_results import (
    RAW,
    RESULTS,
    count,
    gate,
    measurement_counts,
    results,
    slots,
)


def test_results_file_is_current(tmp_path: Path) -> None:
    generated = json.dumps(results(RAW), indent=2, sort_keys=True) + "\n"
    if generated != RESULTS.read_text(encoding="utf-8"):
        regenerated = tmp_path / RESULTS.name
        regenerated.write_text(generated, encoding="utf-8")
        pytest.fail(
            f"{RESULTS} is not what the raw readings produce; "
            f"the regenerated file is {regenerated}"
        )


@pytest.mark.parametrize(
    ("outcomes", "stopped", "expected"),
    [
        (["valid"] * 99 + ["parse_error"], False, (100, 99, 1, 0, "pass")),
        (
            ["valid"] * 98 + ["parse_error", "schema_error"],
            False,
            (100, 98, 2, 0, "fail"),
        ),
        (
            ["valid"] * 98 + ["parse_error", "transport_error"],
            False,
            (100, 98, 1, 1, "undecided by transport"),
        ),
        (["parse_error", "schema_error"], True, (2, 0, 2, 0, "fail")),
        (
            ["parse_error", "valid"],
            True,
            (2, 1, 1, 0, "stopped by environment, undecided"),
        ),
    ],
)
def test_the_gate_counts_against_the_measurements_it_has(
    outcomes: list[str], stopped: bool, expected: tuple[int, int, int, int, str]
) -> None:
    verdict = gate(outcomes, stopped=stopped)
    assert (verdict.n, verdict.k, verdict.s, verdict.t, verdict.verdict) == expected


def test_an_environment_slot_stops_the_run_and_is_not_counted() -> None:
    counts = measurement_counts(["valid", "valid", "environment"])
    assert counts["environment"] == 1
    assert (counts["valid"]["k"], counts["valid"]["n"]) == (2, 2)
    assert counts["verdict"] == "stopped by environment, undecided"


@pytest.mark.parametrize(
    "failure",
    [
        "parse_error",
        "schema_error",
        "blank_content",
        "hidden_re_request",
        "exception_other",
    ],
)
def test_every_judge_failure_counts_in_s(failure: str) -> None:
    verdict = gate(["valid"] * 99 + [failure], stopped=False)
    assert (verdict.k, verdict.s, verdict.t) == (99, 1, 0)


@pytest.mark.parametrize("failure", ["transport_error", "case_timeout"])
def test_every_transport_failure_counts_in_t(failure: str) -> None:
    verdict = gate(["valid"] * 99 + [failure], stopped=False)
    assert (verdict.k, verdict.s, verdict.t) == (99, 0, 1)


def test_a_re_measured_slot_ends_with_its_re_measurement() -> None:
    def row(metric: str, outcome: str, remeasured: bool) -> dict[str, object]:
        return {
            "case_id": "q01",
            "repeat": 1,
            "metric": metric,
            "outcome": outcome,
            "remeasured": remeasured,
        }

    measurements = [
        row("faithfulness", "transport_error", False),
        row("geval", "valid", False),
        row("faithfulness", "valid", True),
    ]
    run_slots = slots(measurements)
    assert run_slots == [
        {"slot": "q01-r1-faithfulness", "first": "transport_error", "final": "valid"},
        {"slot": "q01-r1-geval", "first": "valid", "final": "valid"},
    ]
    counts = measurement_counts([slot["final"] for slot in run_slots])
    assert (counts["valid"]["k"], counts["t"]) == (2, 0)


def test_the_wilson_interval_matches_its_closed_form() -> None:
    assert count(100, 100) == {
        "k": 100,
        "n": 100,
        "percent": 100,
        "interval": [0.963, 1.0],
    }
    assert count(92, 100) == {
        "k": 92,
        "n": 100,
        "percent": 92,
        "interval": [0.85, 0.9589],
    }


def test_the_figures_match_hand_counts_of_the_raw_readings() -> None:
    runs = results(RAW)["runs"]
    first_run = runs["first-run"]["measurements"]
    assert (first_run["valid"]["k"], first_run["valid"]["n"], first_run["t"]) == (
        92,
        100,
        8,
    )
    assert runs["rerun"]["transport"]["attempts_after_a_429"] == {
        "new_connection": 291,
        "reused_connection": 2,
    }
    handshakes = runs["diagnostic"]["transport"][
        "handshake_failures_of_new_connections"
    ]
    assert (handshakes["k"], handshakes["n"], handshakes["percent"]) == (47, 318, 15)
    assert runs["rerun"]["cost"]["estimate_usd"]["steps"]["Truths"] == 0.001001
