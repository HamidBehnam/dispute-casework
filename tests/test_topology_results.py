import json
from pathlib import Path

from dispute_casework.topology_results import (
    RAW,
    RESULTS,
    attributed_to,
    cold_start,
    cost,
    gates,
    ingress_from_inside,
    replicas_the_minute_before,
    results,
    survived,
    warm_window,
)


def run(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "leg": "ingress",
        "arm": "heartbeat",
        "run": 1,
        "seconds": 300,
        "status": 200,
        "duration_s": 300.4,
        "bytes": 927,
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
        attributed_to(
            cut(leg="cloudflare", duration_s=240.5), cut(duration_s=241.1), None
        )
        == "ingress"
    )
    assert (
        attributed_to(
            cut(leg="cloudflare", duration_s=60.0), cut(duration_s=241.1), None
        )
        == "unattributed"
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
    assert not survived(run(bytes=0, max_gap_s=None))


def test_gates_count_heartbeat_runs_per_leg_within_a_series() -> None:
    stream = [
        run(survived=True),
        run(survived=False),
        run(leg="cloudflare", survived=True),
        run(leg="cloudflare", arm="silent", survived=False),
        run(leg="cloudflare", series="paired", survived=False),
        run(leg="ingress-inside", status=403, survived=False),
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


def test_cold_start_runs_are_ordered_by_run_and_valid_only_with_status_200(
    tmp_path: Path,
) -> None:
    (tmp_path / "cold_start").mkdir()
    (tmp_path / "metrics").mkdir()
    (tmp_path / "logs").mkdir()
    for run, status in ((1, 200), (10, 200), (2, 524)):
        record = {
            "run": run,
            "idle_window_s": 600,
            "replicas_before": 0,
            "started_at": "2026-11-03T00:27:29+00:00",
            "status": status,
            "t_headers_s": 50.0,
            "t_first_chunk_s": 50.0,
        }
        (tmp_path / "cold_start" / f"run-{run}.json").write_text(json.dumps(record))
        (tmp_path / "metrics" / f"run-{run}.json").write_text(
            json.dumps([{"t": "2026-11-03T00:26:00Z", "replicas": 0.0}])
        )
    runs = cold_start(tmp_path)
    assert [r["run"] for r in runs] == [1, 2, 10]
    assert [r["valid"] for r in runs] == [True, False, True]


def test_the_minute_before_the_request_is_read_from_the_replicas_series() -> None:
    series = [
        {"t": "2026-11-02T20:05:00Z", "replicas": 0.0},
        {"t": "2026-11-02T20:06:00Z", "replicas": 1.0},
    ]
    assert replicas_the_minute_before(series, "2026-11-02T20:06:41+00:00") == 0
    assert replicas_the_minute_before(series, "2026-11-02T20:07:41+00:00") == 1
    assert replicas_the_minute_before(series, "2026-11-02T20:09:41+00:00") is None
    assert replicas_the_minute_before(None, "2026-11-02T20:06:41+00:00") is None


def test_the_runs_from_inside_are_counted_as_refusals_not_as_a_gate() -> None:
    stream = [
        run(leg="ingress-inside", status=403, ended_by="status", survived=False),
        run(leg="ingress-inside", survived=True),
    ]
    assert ingress_from_inside(stream) == {"n": 2, "refused": 1}
    assert "ingress-inside" not in gates(stream)


def test_the_warm_window_is_the_time_between_the_two_applies() -> None:
    opened = {"warm_window_started_at": {"utc": "2026-11-03T02:20:08Z"}}
    closed = opened | {"warm_window_ended_at": {"utc": "2026-11-05T21:57:00Z"}}
    assert warm_window(closed) == {
        "start": "2026-11-03T02:20:08Z",
        "end": "2026-11-05T21:57:00Z",
        "hours": 67.61,
    }
    assert warm_window(opened) is None
    assert warm_window({}) is None


def test_the_warm_replicas_month_is_the_window_cost_per_day_of_the_window(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "date": "2026-11-02",
            "resource_type": "microsoft.app/containerapps",
            "usd": 9,
        },
        {
            "date": "2026-11-03",
            "resource_type": "microsoft.app/containerapps",
            "usd": 1,
        },
        {
            "date": "2026-11-04",
            "resource_type": "microsoft.app/containerapps",
            "usd": 2,
        },
        {"date": "2026-11-04", "resource_type": "microsoft.network/dnszones", "usd": 5},
        {
            "date": "2026-11-05",
            "resource_type": "microsoft.app/containerapps",
            "usd": 3,
        },
    ]
    (tmp_path / "cost").mkdir()
    (tmp_path / "cost" / "rows.json").write_text(json.dumps(rows))
    window = {
        "start": "2026-11-03T00:00:00Z",
        "end": "2026-11-05T12:00:00Z",
        "hours": 60.0,
    }
    assert cost(tmp_path, window) == (rows, round(6 / 2.5 * 30.4, 2))
    assert cost(tmp_path, None) == (rows, None)
