import json
from pathlib import Path

import pytest
from judge_results import RAW, RESULTS, gate, measurement_counts, results


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
