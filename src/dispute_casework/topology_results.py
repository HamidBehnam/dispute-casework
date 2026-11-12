"""Writes docs/results/0007-topology.json from the committed raw topology readings.

The raw readings live under docs/results/0007-topology-raw: the measurement
client's records (stream/, cold_start/), the Azure Monitor and Log Analytics
outputs saved per cold-start run (metrics/, logs/) and the hourly replica
readings after the warm window (metrics/after-window.json), the Cost Management query
responses (cost/), the observations made at apply time (observations.json) and
the retail rates (rates.json). Every figure in the results file is computed here
from them.
"""

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from dispute_casework.topology_smoke import max_gap_s

RAW = Path(__file__).parents[2] / "docs/results/0007-topology-raw"
RESULTS = Path(__file__).parents[2] / "docs/results/0007-topology.json"
TIMERS_S = {"ingress": 240, "cloudflare": 125}
WINDOW_BEFORE_S = 5
WINDOW_AFTER_S = 10
GAP_BOUND_S = 20
GATE_LEGS = ("cloudflare", "ingress", "internal")
HOURS_PER_MONTH = 730
SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600
CONTAINER_APPS = "Azure Container Apps"
METER_RESOURCE = {
    "Standard vCPU Idle Usage": "vcpu",
    "Standard vCPU Active Usage": "vcpu",
    "Standard Memory Idle Usage": "gib",
    "Standard Memory Active Usage": "gib",
}
IDLE_METERS = {"Standard vCPU Idle Usage", "Standard Memory Idle Usage"}
MANAGEMENT_METERS = {
    "Environment Management Hour",
    "Environment Planned Maintenance Hour",
    "Environment Private Endpoint",
    "Dedicated Plan Management",
}
WARM_MONTH_ASSUMPTIONS = {
    "meters": "every Container Apps meter billed on the window's days, idle and "
    "active; the last day also holds the minutes until the replica stopped "
    "(replicas_zero_after_window in the raw observations); the billed seconds are "
    "those after the month's grant ran out, and the rows do not say whether the "
    "grant took idle or active seconds first, so their active share is not "
    "established as the window's; idle_only_usd_per_month is the month at the "
    "idle meters' unit prices alone",
    "free_grant": "not subtracted: the rows carry no free usage, in cost or in "
    "quantity (the vendor: free usage does not appear on the bill), so the billed "
    "hours are the window's hours past the month's grant and each meter's "
    "usd_per_unit is its full unit price",
    "estimate": "the rows are Cost Management's open-month estimates until the "
    "invoice is issued; the estimate's highest-tier pricing does not move them, "
    "since each of these meters has one price tier",
    "allocation": "billed at the window's allocation; another allocation scales "
    "each vCPU meter by its vCPU and each memory meter by its memory",
    "month": "730 hours, no free grant",
}
OFFSETS_RESOLUTION = (
    "started_at is recorded truncated to the second and a system log row's time "
    "is an upper bound of its event, so scheduled_s and last_container_started_s "
    "are overstated and started_to_headers_s understated, by up to 1 s for the "
    "truncation plus the rows' batching delay"
)


def timer_window(duration_s: float) -> str | None:
    """The timer whose window [T - 5 s, T + 10 s] the end time falls in."""
    for timer, limit in TIMERS_S.items():
        if limit - WINDOW_BEFORE_S <= duration_s <= limit + WINDOW_AFTER_S:
            return timer
    return None


def outlasted(pair: dict[str, Any], run: dict[str, Any]) -> bool:
    """Whether the run of the same arm and order on another leg got past this run's end.

    The heartbeat arm must survive; the silent arm never meets the gap bound, so
    for it the pre-registered comparison is lasting longer than the cut.
    """
    if run["arm"] == "heartbeat":
        return bool(pair["survived"])
    return bool(pair["duration_s"] > run["duration_s"])


def attributed_to(
    run: dict[str, Any],
    ingress_pair: dict[str, Any] | None,
    internal_pair: dict[str, Any] | None,
) -> str:
    """What ended a run that did not reach its end.

    Off the Cloudflare leg, the timer whose window the end time falls in. On it,
    Cloudflare only when the Ingress run of the same arm and order outlasted the
    cut, and for the silent arm only inside Cloudflare's window; when both legs
    failed, the ingress or the application, which the Internal run of the same
    arm and order decides.
    """
    if run["ended_by"] == "server" and run["duration_s"] >= run["seconds"]:
        return "none"
    window = timer_window(run["duration_s"])
    silent_outside_cloudflare = run["arm"] == "silent" and window != "cloudflare"
    if run["leg"] != "cloudflare" or silent_outside_cloudflare:
        return window or "unattributed"
    if ingress_pair is None:
        return "pending"
    if outlasted(ingress_pair, run):
        return "cloudflare"
    if internal_pair is None:
        return "ingress or application"
    return "ingress" if outlasted(internal_pair, run) else "application"


def survived(run: dict[str, Any]) -> bool:
    return bool(
        run["status"] == 200
        and run["ended_by"] == "server"
        and run["duration_s"] >= run["seconds"]
        and run["bytes"] > 0
        and run["max_gap_s"] <= GAP_BOUND_S
    )


def attribute(stream: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gap and survival of every run, then its attribution against its pairs.

    The gap is computed from the record's arrival times by the measurement client's
    rule rather than read from the record, so every record is held to the same rule.
    """
    for run in stream:
        run["max_gap_s"] = max_gap_s(
            run["t_headers_s"], run["chunks"], run["duration_s"]
        )
        run["survived"] = survived(run)
    pairs = {
        (run["leg"], run["arm"], run["run"]): run
        for run in stream
        if run.get("series") is None
    }
    for run in stream:
        run["attributed_to"] = attributed_to(
            run,
            pairs.get(("ingress", run["arm"], run["run"])),
            pairs.get(("internal", run["arm"], run["run"])),
        )
    return stream


def gates(
    stream: list[dict[str, Any]], series: str | None = None
) -> dict[str, dict[str, int]]:
    """k of n heartbeat runs that survived, per gated leg, within one series."""
    counts: dict[str, dict[str, int]] = {}
    for run in stream:
        if (
            run["arm"] != "heartbeat"
            or run.get("series") != series
            or run["leg"] not in GATE_LEGS
        ):
            continue
        leg = counts.setdefault(run["leg"], {"k": 0, "n": 0})
        leg["n"] += 1
        leg["k"] += int(run["survived"])
    for leg in counts.values():
        leg["percent"] = round(100 * leg["k"] / leg["n"])
    return counts


def ingress_from_inside(stream: list[dict[str, Any]]) -> dict[str, int]:
    """The job's runs through the app FQDN, which the allow-list should refuse."""
    runs = [r for r in stream if r["leg"] == "ingress-inside"]
    return {"n": len(runs), "refused": sum(r["status"] == 403 for r in runs)}


def ingress_timeout(stream: list[dict[str, Any]]) -> str:
    """Claim 2, from the Ingress leg."""
    heartbeat = [r for r in stream if r["leg"] == "ingress" and r["arm"] == "heartbeat"]
    silent = [r for r in stream if r["leg"] == "ingress" and r["arm"] == "silent"]
    if not heartbeat or not silent:
        return "pending"
    if any(r["attributed_to"] == "ingress" for r in heartbeat):
        return "total"
    if all(r["survived"] for r in heartbeat):
        if all(r["attributed_to"] == "ingress" for r in silent):
            return "idle"
        if all(r["attributed_to"] == "none" for r in silent):
            return "no timer observed"
    return "unattributed"


def cloudflare_timeout(stream: list[dict[str, Any]]) -> str:
    """Claim 3: the silent arm through Cloudflare against its Ingress run."""
    cloudflare = [
        r for r in stream if r["leg"] == "cloudflare" and r["arm"] == "silent"
    ]
    ingress = [r for r in stream if r["leg"] == "ingress" and r["arm"] == "silent"]
    if not cloudflare or not ingress:
        return "pending"
    if all(r["attributed_to"] == "cloudflare" for r in cloudflare):
        return "governs body gaps"
    if all(
        r["duration_s"] > TIMERS_S["cloudflare"] + WINDOW_AFTER_S for r in cloudflare
    ):
        return "first response only"
    return "unattributed"


def replicas_the_minute_before(
    series: list[dict[str, Any]] | None, started_at: str
) -> float | None:
    """The Replicas maximum for the minute before the request, if the series has it."""
    minute = datetime.fromisoformat(started_at).replace(second=0) - timedelta(minutes=1)
    for point in series or []:
        if datetime.fromisoformat(point["t"]) == minute:
            return float(point["replicas"])
    return None


def offsets(
    started_at: str, rows: list[dict[str, Any]] | None, t_headers_s: float | None
) -> dict[str, float | None]:
    """Where a cold start's time goes, from the system log rows' timestamps.

    The request's start is recorded truncated to the second, so it precedes the
    true start by up to 1 s, and the platform writes the rows in batches, so a
    row's time is an upper bound of its event. Both make an offset from the
    request larger than its event's, and started_to_headers_s, whose headers time
    the client takes from the true start, smaller.
    """
    t0 = datetime.fromisoformat(started_at)

    def seconds_until(reason: str, last: bool) -> float | None:
        times = [
            datetime.fromisoformat(row["t"])
            for row in rows or []
            if row["reason"] == reason
        ]
        if not times:
            return None
        return round(((max if last else min)(times) - t0).total_seconds(), 3)

    scheduled = seconds_until("AssigningReplica", last=False)
    started = seconds_until("ContainerStarted", last=True)
    return {
        "scheduled_s": scheduled,
        "last_container_started_s": started,
        "started_to_headers_s": None
        if started is None or t_headers_s is None
        else round(t_headers_s - started, 3),
    }


def cold_start(raw: Path, series: str | None = None) -> list[dict[str, Any]]:
    """The repetitions of one series, each with the vendor readings of its name."""
    runs = []
    for path in sorted((raw / "cold_start").glob("*.json")):
        record = json.loads(path.read_text())
        if record.get("series") != series:
            continue
        metric = optional_json(raw / "metrics" / path.name)
        rows = optional_json(raw / "logs" / path.name)
        runs.append(
            {
                "run": record["run"],
                "idle_window_s": record["idle_window_s"],
                "replicas_before": record["replicas_before"],
                "t_headers_s": record["t_headers_s"],
                "t_first_chunk_s": record["t_first_chunk_s"],
                "replicas_series": metric,
                "system_log_rows": rows,
                "offsets": offsets(record["started_at"], rows, record["t_headers_s"]),
                "valid": record["status"] == 200
                and record["replicas_before"] == 0
                and replicas_the_minute_before(metric, record["started_at"]) == 0,
            }
        )
    return sorted(runs, key=lambda run: run["run"])


def median_of(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(median(present), 3) if present else None


def cold_start_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [run for run in runs if run["valid"]]
    offsets = [run["offsets"] for run in valid]
    return {
        "valid_k": len(valid),
        "n": len(runs),
        "median_t_headers_s": median_of(run["t_headers_s"] for run in valid),
        "median_t_first_chunk_s": median_of(run["t_first_chunk_s"] for run in valid),
        "median_scheduled_s": median_of(o["scheduled_s"] for o in offsets),
        "median_last_container_started_s": median_of(
            o["last_container_started_s"] for o in offsets
        ),
        "median_started_to_headers_s": median_of(
            o["started_to_headers_s"] for o in offsets
        ),
    }


def projections(rates: dict[str, Any] | None) -> dict[str, Any]:
    """Monthly figures from the retail rates and free grants."""
    if rates is None:
        return {}
    idle_floor = {}
    for allocation in rates["allocations"]:
        vcpu_s = allocation["vcpu"] * SECONDS_PER_MONTH - rates["free_vcpu_seconds"]
        gib_s = allocation["gib"] * SECONDS_PER_MONTH - rates["free_gib_seconds"]
        usd = (
            vcpu_s * rates["vcpu_idle_usd_per_second"]
            + gib_s * rates["gib_usd_per_second"]
        )
        idle_floor[f"{allocation['vcpu']}/{allocation['gib']}"] = round(usd, 2)
    return {
        "environment_meter_usd_per_month": round(
            rates["environment_management_usd_per_hour"] * SECONDS_PER_MONTH / 3600, 2
        ),
        "idle_floor_usd_per_month": idle_floor,
    }


def warm_window(observations: dict[str, Any]) -> dict[str, Any] | None:
    """The warm window between the apply that opened it and the apply that closed it."""
    if not {"warm_window_started_at", "warm_window_ended_at"} <= observations.keys():
        return None
    start = observations["warm_window_started_at"]["utc"]
    end = observations["warm_window_ended_at"]["utc"]
    seconds = (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds()
    return {"start": start, "end": end, "hours": round(seconds / 3600, 2)}


def cost(raw: Path) -> list[dict[str, Any]]:
    """The Query API responses as returned, one row per day, service and meter.

    The query sums Cost and UsageQuantity per day grouped by the ServiceName and
    Meter dimensions; UsageDate is a number, yyyymmdd. Every figure here is in US
    dollars, so a row in another currency is refused rather than summed.
    """
    rows = []
    for path in sorted((raw / "cost").glob("*.json")):
        body = json.loads(path.read_text())["properties"]
        names = [column["name"] for column in body["columns"]]
        for values in body["rows"]:
            row = dict(zip(names, values, strict=True))
            if row["Currency"] != "USD":
                raise ValueError(f"{path.name}: a row in {row['Currency']}, not USD")
            rows.append(
                {
                    "date": datetime.strptime(str(row["UsageDate"]), "%Y%m%d")
                    .date()
                    .isoformat(),
                    "service": row["ServiceName"],
                    "meter": row["Meter"],
                    "quantity": row["UsageQuantity"],
                    "usd": row["Cost"],
                }
            )
    return rows


def environment_meter(rows: list[dict[str, Any]]) -> str:
    """Claim 1: whether a plan management meter billed, from the rows."""
    if not rows:
        return "pending the cost read"
    billed = any(
        row["service"] == CONTAINER_APPS and row["meter"] in MANAGEMENT_METERS
        for row in rows
    )
    return "yes" if billed else "no"


def warm_window_cost(
    rows: list[dict[str, Any]], window: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The app's cost and per-meter quantities over the window's whole days."""
    if window is None or not rows:
        return None
    first, last = window["start"][:10], window["end"][:10]
    meters: dict[str, dict[str, float]] = {}
    for row in rows:
        if row["service"] == CONTAINER_APPS and first <= row["date"] <= last:
            meter = meters.setdefault(row["meter"], {"quantity": 0.0, "usd": 0.0})
            meter["quantity"] += row["quantity"]
            meter["usd"] += row["usd"]
    return {
        "from": first,
        "to": last,
        "days": "whole calendar days; the boundary days are included whole",
        "usd": round(sum(meter["usd"] for meter in meters.values()), 6),
        "meters": {
            name: {
                "quantity": round(meter["quantity"], 3),
                "usd": round(meter["usd"], 6),
                "usd_per_unit": round(meter["usd"] / meter["quantity"], 12)
                if meter["quantity"]
                else None,
            }
            for name, meter in sorted(meters.items())
        },
    }


def warm_replica_month(
    window_cost: dict[str, Any] | None,
    window_allocation: dict[str, float] | None,
    rates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The month at the hourly rate the window's billed amount and hours give.

    The billed hours are the billed vCPU-seconds over the window's vCPU.
    """
    if window_cost is None or window_allocation is None or rates is None:
        return None
    vcpu_seconds = 0.0
    usd_by_resource: dict[str | None, float] = {}
    for name, meter in window_cost["meters"].items():
        resource = METER_RESOURCE.get(name)
        usd_by_resource[resource] = usd_by_resource.get(resource, 0.0) + meter["usd"]
        if resource == "vcpu":
            vcpu_seconds += meter["quantity"]
    if not vcpu_seconds:
        return None
    hours = vcpu_seconds / window_allocation["vcpu"] / 3600
    usd_per_month = {
        f"{allocation['vcpu']}/{allocation['gib']}": round(
            sum(
                usd * (allocation[r] / window_allocation[r] if r else 1.0)
                for r, usd in usd_by_resource.items()
            )
            / hours
            * HOURS_PER_MONTH,
            2,
        )
        for allocation in rates["allocations"]
    }
    idle = {
        METER_RESOURCE[name]: meter["usd"] / meter["quantity"]
        for name, meter in window_cost["meters"].items()
        if name in IDLE_METERS and meter["quantity"]
    }
    idle_only_usd_per_month = (
        {
            f"{allocation['vcpu']}/{allocation['gib']}": round(
                SECONDS_PER_MONTH * sum(allocation[r] * idle[r] for r in idle), 2
            )
            for allocation in rates["allocations"]
        }
        if len(idle) == len(IDLE_METERS)
        else None
    )
    return {
        "window_allocation": f"{window_allocation['vcpu']}/{window_allocation['gib']}",
        "billed_usd": window_cost["usd"],
        "billed_hours": round(hours, 3),
        "usd_per_hour": round(window_cost["usd"] / hours, 6),
        "usd_per_month": usd_per_month,
        "idle_only_usd_per_month": idle_only_usd_per_month,
        "assumptions": WARM_MONTH_ASSUMPTIONS,
    }


def zero_replica_floor(
    rows: list[dict[str, Any]], hourly_replicas: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """What the resource group is billed per day while the app runs no replica.

    A day counts when all 24 of its hourly Replicas maxima read 0.
    """
    if not rows or not hourly_replicas:
        return None
    by_day: dict[str, list[float | None]] = {}
    for point in hourly_replicas:
        by_day.setdefault(point["t"][:10], []).append(point["replicas"])
    days = sorted(
        day
        for day, replicas in by_day.items()
        if len(replicas) == 24 and all(r == 0 for r in replicas)
    )
    if not days:
        return None
    services: dict[str, float] = {}
    for row in rows:
        if row["date"] in days:
            services[row["service"]] = services.get(row["service"], 0.0) + row["usd"]
    usd_per_day = sum(services.values()) / len(days)
    return {
        "from": days[0],
        "to": days[-1],
        "days": len(days),
        "container_apps_usd": round(services.get(CONTAINER_APPS, 0.0), 6),
        "usd_per_day_by_service": {
            service: round(usd / len(days), 6)
            for service, usd in sorted(services.items())
        },
        "usd_per_day": round(usd_per_day, 6),
        "usd_per_month": round(usd_per_day * HOURS_PER_MONTH / 24, 2),
        "free_offers": "a service at 0.0 is billed on free-offer meters or within "
        "a free allowance on these days, so the floor is what is billed under "
        "those offers, not the components' list price",
    }


def optional_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def results(raw: Path) -> dict[str, Any]:
    stream = attribute(
        [
            {
                k: v
                for k, v in json.loads(path.read_text()).items()
                if k not in {"idle_window_s", "replicas_before"}
            }
            for path in sorted((raw / "stream").glob("*.json"))
        ]
    )
    runs = cold_start(raw)
    probes = cold_start(raw, "probes")
    observations = optional_json(raw / "observations.json") or {}
    rates = optional_json(raw / "rates.json")
    rows = cost(raw)
    window = warm_window(observations)
    window_cost = warm_window_cost(rows, window)
    return {
        "cold_start": runs,
        "cold_start_summary": cold_start_summary(runs),
        "cold_start_probes": probes,
        "cold_start_probes_summary": cold_start_summary(probes),
        "stream": stream,
        "gates": gates(stream),
        "gates_paired": gates(stream, "paired"),
        "ingress_inside": ingress_from_inside(stream),
        "cold_start_offsets_resolution": OFFSETS_RESOLUTION,
        "claims": {
            "environment_meter": environment_meter(rows),
            "ingress_timeout": ingress_timeout(stream),
            "cloudflare_timeout": cloudflare_timeout(stream),
            "sub_quarter_vcpu": observations.get("sub_quarter_vcpu", "pending"),
            "custom_domain_binding": observations.get(
                "custom_domain_binding", "pending"
            ),
            "prevent_destroy_on_count": observations.get(
                "prevent_destroy_on_count", "pending"
            ),
            "cerbos_empty_policies": observations.get(
                "cerbos_empty_policies", "pending"
            ),
        },
        "projections": projections(rates),
        "cost": rows,
        "warm_window": window,
        "warm_window_cost": window_cost,
        "warm_replica_usd_per_month": warm_replica_month(
            window_cost, observations.get("warm_window_allocation"), rates
        ),
        "zero_replica_floor": zero_replica_floor(
            rows, optional_json(raw / "metrics" / "after-window.json")
        ),
    }


def main() -> None:
    RESULTS.write_text(json.dumps(results(RAW), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
