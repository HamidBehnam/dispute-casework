import json
from pathlib import Path

from dispute_casework.topology_results import (
    RAW,
    RESULTS,
    attributed_to,
    cold_start,
    cold_start_summary,
    cost,
    gates,
    ingress_from_inside,
    offsets,
    replicas_the_minute_before,
    results,
    survived,
    warm_replica_month,
    warm_window,
    warm_window_cost,
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
    for name, run, status, series in (
        ("run-1", 1, 200, None),
        ("run-10", 10, 200, None),
        ("run-2", 2, 524, None),
        ("probes-run-1", 1, 200, "probes"),
    ):
        record = {
            "run": run,
            "series": series,
            "idle_window_s": 600,
            "replicas_before": 0,
            "started_at": "2026-11-03T00:27:29+00:00",
            "status": status,
            "t_headers_s": 50.0,
            "t_first_chunk_s": 50.0,
        }
        (tmp_path / "cold_start" / f"{name}.json").write_text(json.dumps(record))
        (tmp_path / "metrics" / f"{name}.json").write_text(
            json.dumps([{"t": "2026-11-03T00:26:00Z", "replicas": 0.0}])
        )
    runs = cold_start(tmp_path)
    assert [r["run"] for r in runs] == [1, 2, 10]
    assert [r["valid"] for r in runs] == [True, False, True]
    assert runs[0]["offsets"] == {
        "scheduled_s": None,
        "last_container_started_s": None,
        "started_to_headers_s": None,
    }
    assert [r["run"] for r in cold_start(tmp_path, "probes")] == [1]
    assert cold_start_summary(runs) == {
        "valid_k": 2,
        "n": 3,
        "median_t_headers_s": 50.0,
        "median_t_first_chunk_s": 50.0,
        "median_scheduled_s": None,
        "median_last_container_started_s": None,
        "median_started_to_headers_s": None,
    }


def test_the_offsets_are_read_from_the_scheduling_and_last_start_rows() -> None:
    rows = [
        {"reason": "AssigningReplica", "t": "2026-11-03T00:27:31.0227384Z"},
        {"reason": "PulledImage", "t": "2026-11-03T00:27:53.0240000Z"},
        {"reason": "ContainerStarted", "t": "2026-11-03T00:27:54.0140000Z"},
        {"reason": "ContainerStarted", "t": "2026-11-03T00:27:56.0680000Z"},
        {"reason": "ProbeFailed", "t": "2026-11-03T00:27:57.0320000Z"},
    ]
    assert offsets("2026-11-03T00:27:29+00:00", rows, 59.339) == {
        "scheduled_s": 2.023,
        "last_container_started_s": 27.068,
        "started_to_headers_s": 32.271,
    }
    assert offsets("2026-11-03T00:27:29+00:00", None, 59.339) == {
        "scheduled_s": None,
        "last_container_started_s": None,
        "started_to_headers_s": None,
    }


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


def query_response(rows: list[list[object]]) -> dict[str, object]:
    """The shape of the Query API's example response (api-version 2025-03-01)."""
    return {
        "type": "microsoft.costmanagement/Query",
        "properties": {
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "UsageQuantity", "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "Meter", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "nextLink": None,
            "rows": rows,
        },
    }


WINDOW = {
    "start": "2026-11-03T02:20:08Z",
    "end": "2026-11-05T21:57:00Z",
    "hours": 67.61,
}
RATES = {"allocations": [{"vcpu": 0.75, "gib": 1.5}, {"vcpu": 1.0, "gib": 2.0}]}


def test_cost_rows_are_read_from_the_query_response_as_returned(
    tmp_path: Path,
) -> None:
    (tmp_path / "cost").mkdir()
    (tmp_path / "cost" / "query.json").write_text(
        json.dumps(
            query_response(
                [
                    [0.3, 100000.0, 20261103, "Standard vCPU Idle Usage", "USD"],
                    [2.10333307059661, 5.0, 20261102, "Standard Requests", "USD"],
                ]
            )
        )
    )
    assert cost(tmp_path) == [
        {
            "date": "2026-11-03",
            "meter": "Standard vCPU Idle Usage",
            "quantity": 100000.0,
            "usd": 0.3,
        },
        {
            "date": "2026-11-02",
            "meter": "Standard Requests",
            "quantity": 5.0,
            "usd": 2.10333307059661,
        },
    ]


def test_a_response_with_no_rows_yields_no_cost_and_no_month(tmp_path: Path) -> None:
    (tmp_path / "cost").mkdir()
    (tmp_path / "cost" / "query.json").write_text(json.dumps(query_response([])))
    assert cost(tmp_path) == []
    assert warm_window_cost([], WINDOW) is None
    assert warm_replica_month(None, RATES) is None


def test_the_window_cost_sums_its_whole_days_per_meter_and_the_month_follows() -> None:
    rows = [
        {
            "date": "2026-11-02",
            "meter": "Standard vCPU Idle Usage",
            "quantity": 9.0,
            "usd": 9.0,
        },
        {
            "date": "2026-11-03",
            "meter": "Standard vCPU Idle Usage",
            "quantity": 40000.0,
            "usd": 0.12,
        },
        {
            "date": "2026-11-04",
            "meter": "Standard vCPU Idle Usage",
            "quantity": 60000.0,
            "usd": 0.18,
        },
        {
            "date": "2026-11-04",
            "meter": "Standard Memory Idle Usage",
            "quantity": 200000.0,
            "usd": 0.6,
        },
        {
            "date": "2026-11-05",
            "meter": "Standard Requests",
            "quantity": 0.0,
            "usd": 0.0,
        },
        {
            "date": "2026-11-06",
            "meter": "Standard Memory Idle Usage",
            "quantity": 7.0,
            "usd": 7.0,
        },
    ]
    window_cost = warm_window_cost(rows, WINDOW)
    assert window_cost == {
        "from": "2026-11-03",
        "to": "2026-11-05",
        "days": "whole calendar days; the boundary days are included whole",
        "usd": 0.9,
        "meters": {
            "Standard Memory Idle Usage": {
                "quantity": 200000.0,
                "usd": 0.6,
                "usd_per_unit": 3e-06,
            },
            "Standard Requests": {"quantity": 0.0, "usd": 0.0, "usd_per_unit": None},
            "Standard vCPU Idle Usage": {
                "quantity": 100000.0,
                "usd": 0.3,
                "usd_per_unit": 3e-06,
            },
        },
    }
    month = warm_replica_month(window_cost, RATES)
    assert month is not None
    assert month["by_allocation"] == {"0.75/1.5": 17.74, "1.0/2.0": 23.65}
    assert set(month["assumptions"]) == {"meters", "free_grant", "month"}
    assert warm_replica_month(window_cost, None) is None


def test_the_month_needs_a_measured_price_for_both_idle_meters() -> None:
    rows = [
        {
            "date": "2026-11-04",
            "meter": "Standard vCPU Idle Usage",
            "quantity": 100000.0,
            "usd": 0.3,
        },
    ]
    assert warm_replica_month(warm_window_cost(rows, WINDOW), RATES) is None
