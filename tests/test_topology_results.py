import json
from pathlib import Path

from dispute_casework.topology_results import (
    RAW,
    RESULTS,
    attributed_to,
    gates,
    results,
    survived,
)


def run(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "leg": "ingress",
        "arm": "heartbeat",
        "seconds": 300,
        "status": 200,
        "duration_s": 300.4,
        "max_gap_s": 15.2,
        "ended_by": "server",
    }
    return record | overrides


def test_a_silent_run_is_attributed_by_its_end_time() -> None:
    assert attributed_to(240.0, 300, "reset") == "ingress"
    assert attributed_to(235.0, 300, "reset") == "ingress"
    assert attributed_to(250.0, 300, "reset") == "ingress"
    assert attributed_to(124.0, 300, "reset") == "cloudflare"
    assert attributed_to(200.0, 300, "reset") == "unattributed"
    assert attributed_to(300.2, 300, "server") == "none"


def test_a_run_survives_only_within_the_gap_bound_for_its_whole_duration() -> None:
    assert survived(run())
    assert not survived(run(max_gap_s=20.5))
    assert not survived(run(duration_s=299.0))
    assert not survived(run(ended_by="reset"))
    assert not survived(run(status=524))


def test_gates_count_heartbeat_runs_per_leg() -> None:
    stream = [
        run(survived=True),
        run(survived=False),
        run(leg="cloudflare", survived=True),
        run(leg="cloudflare", arm="silent", survived=False),
    ]
    assert gates(stream) == {
        "ingress": {"k": 1, "n": 2, "percent": 50},
        "cloudflare": {"k": 1, "n": 1, "percent": 100},
    }


def test_results_file_is_current(tmp_path: Path) -> None:
    """The committed results are what the committed raw readings produce."""
    regenerated = tmp_path / RESULTS.name
    regenerated.write_text(json.dumps(results(RAW), indent=2, sort_keys=True) + "\n")
    assert RESULTS.read_text() == regenerated.read_text(), f"regenerated: {regenerated}"
