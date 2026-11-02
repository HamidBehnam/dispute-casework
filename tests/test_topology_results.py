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
        "run": 1,
        "seconds": 300,
        "status": 200,
        "duration_s": 300.4,
        "max_gap_s": 15.2,
        "ended_by": "server",
    }
    return record | overrides


def cut(**overrides: object) -> dict[str, object]:
    return run(arm="silent", ended_by="reset", max_gap_s=0.0) | overrides


def test_an_ingress_run_is_attributed_by_its_end_time() -> None:
    assert attributed_to(cut(duration_s=240.0), None, None) == "ingress"
    assert attributed_to(cut(duration_s=235.0), None, None) == "ingress"
    assert attributed_to(cut(duration_s=250.0), None, None) == "ingress"
    assert attributed_to(cut(duration_s=124.0), None, None) == "cloudflare"
    assert attributed_to(cut(duration_s=200.0), None, None) == "unattributed"
    assert attributed_to(run(duration_s=300.2), None, None) == "none"


def test_a_cloudflare_failure_is_attributed_against_the_runs_of_its_order() -> None:
    silent = cut(leg="cloudflare", duration_s=125.2)
    assert attributed_to(silent, None, None) == "pending"
    assert attributed_to(silent, cut(duration_s=240.3), None) == "cloudflare"
    assert (
        attributed_to(silent, cut(duration_s=125.1), None) == "ingress or application"
    )
    assert (
        attributed_to(silent, cut(duration_s=125.1), cut(duration_s=300.1)) == "ingress"
    )
    assert (
        attributed_to(silent, cut(duration_s=125.1), cut(duration_s=125.0))
        == "application"
    )
    heartbeat = run(leg="cloudflare", duration_s=90.3, ended_by="reset")
    assert attributed_to(heartbeat, run(survived=True), None) == "cloudflare"
    assert attributed_to(heartbeat, run(survived=False, duration_s=200.0), None) == (
        "ingress or application"
    )


def test_committed_cloudflare_attributions_rest_on_an_ingress_run_that_outlasted() -> (
    None
):
    stream = results(RAW)["stream"]
    for record in stream:
        if record["leg"] == "cloudflare" and record["attributed_to"] == "cloudflare":
            pair = next(
                (
                    r
                    for r in stream
                    if r["leg"] == "ingress"
                    and r["arm"] == record["arm"]
                    and r["run"] == record["run"]
                ),
                None,
            )
            assert pair is not None, record["started_at"]
            assert pair["survived"] or pair["duration_s"] > record["duration_s"]


def test_a_run_survives_only_within_the_gap_bound_for_its_whole_duration() -> None:
    assert survived(run())
    assert not survived(run(max_gap_s=20.5))
    assert not survived(run(duration_s=299.0))
    assert not survived(run(ended_by="reset"))
    assert not survived(run(status=524))


def test_gates_count_heartbeat_runs_per_leg_within_a_series() -> None:
    stream = [
        run(survived=True),
        run(survived=False),
        run(leg="cloudflare", survived=True),
        run(leg="cloudflare", arm="silent", survived=False),
        run(leg="cloudflare", series="paired", survived=False),
    ]
    assert gates(stream) == {
        "ingress": {"k": 1, "n": 2, "percent": 50},
        "cloudflare": {"k": 1, "n": 1, "percent": 100},
    }
    assert gates(stream, "paired") == {"cloudflare": {"k": 0, "n": 1, "percent": 0}}


def test_results_file_is_current(tmp_path: Path) -> None:
    """The committed results are what the committed raw readings produce."""
    regenerated = tmp_path / RESULTS.name
    regenerated.write_text(json.dumps(results(RAW), indent=2, sort_keys=True) + "\n")
    assert RESULTS.read_text() == regenerated.read_text(), f"regenerated: {regenerated}"
