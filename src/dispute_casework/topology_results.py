"""Writes docs/results/0007-topology.json from the committed raw readings of the spike.

The raw readings live under docs/results/0007-topology-raw: the measurement
client's records (stream/, cold_start/), the Azure Monitor and Log Analytics
outputs saved per cold-start run (metrics/, logs/), the Cost Management rows
(cost/), the observations made at apply time (observations.json) and the retail
rates (rates.json). Every figure in the results file is computed here from them.
"""

import json
from pathlib import Path
from statistics import median
from typing import Any

RAW = Path(__file__).parents[2] / "docs/results/0007-topology-raw"
RESULTS = Path(__file__).parents[2] / "docs/results/0007-topology.json"
TIMERS_S = {"ingress": 240, "cloudflare": 125}
WINDOW_BEFORE_S = 5
WINDOW_AFTER_S = 10
GAP_BOUND_S = 20
SECONDS_PER_MONTH = 730 * 3600


def attributed_to(duration_s: float, seconds: int, ended_by: str) -> str:
    """The timer whose window the end time falls in, or what else ended the run."""
    if ended_by == "server" and duration_s >= seconds:
        return "none"
    for timer, limit in TIMERS_S.items():
        if limit - WINDOW_BEFORE_S <= duration_s <= limit + WINDOW_AFTER_S:
            return timer
    return "unattributed"


def survived(run: dict[str, Any]) -> bool:
    return (
        run["status"] == 200
        and run["ended_by"] == "server"
        and run["duration_s"] >= run["seconds"]
        and (run["max_gap_s"] or 0) <= GAP_BOUND_S
    )


def records(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def gates(stream: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """k of n heartbeat runs that survived, per leg."""
    counts: dict[str, dict[str, int]] = {}
    for run in stream:
        if run["arm"] != "heartbeat":
            continue
        leg = counts.setdefault(run["leg"], {"k": 0, "n": 0})
        leg["n"] += 1
        leg["k"] += int(run["survived"])
    for leg in counts.values():
        leg["percent"] = round(100 * leg["k"] / leg["n"])
    return counts


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
    """Claim 3: the silent arm through Cloudflare against the same arm at ingress."""
    cloudflare = [
        r for r in stream if r["leg"] == "cloudflare" and r["arm"] == "silent"
    ]
    ingress = [r for r in stream if r["leg"] == "ingress" and r["arm"] == "silent"]
    if not cloudflare or not ingress:
        return "pending"
    paired = zip(cloudflare, ingress, strict=False)
    if all(
        c["attributed_to"] == "cloudflare" and i["duration_s"] > c["duration_s"]
        for c, i in paired
    ):
        return "governs body gaps"
    if all(
        r["duration_s"] > TIMERS_S["cloudflare"] + WINDOW_AFTER_S for r in cloudflare
    ):
        return "first response only"
    return "unattributed"


def cold_start(raw: Path) -> list[dict[str, Any]]:
    runs = []
    for record in records(raw / "cold_start"):
        run = record["run"]
        runs.append(
            {
                "run": run,
                "idle_window_s": record["idle_window_s"],
                "replicas_before": record["replicas_before"],
                "t_headers_s": record["t_headers_s"],
                "t_first_chunk_s": record["t_first_chunk_s"],
                "replicas_series": optional_json(raw / "metrics" / f"run-{run}.json"),
                "system_log_rows": optional_json(raw / "logs" / f"run-{run}.json"),
                "valid": record["replicas_before"] == 0,
            }
        )
    return runs


def cold_start_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [run for run in runs if run["valid"]]
    return {
        "valid_k": len(valid),
        "n": len(runs),
        "median_t_headers_s": median(r["t_headers_s"] for r in valid)
        if valid
        else None,
        "median_t_first_chunk_s": median(r["t_first_chunk_s"] for r in valid)
        if valid
        else None,
    }


def projections(rates: dict[str, Any] | None) -> dict[str, Any]:
    """Monthly figures from the retail rates and free grants, as 0b computed them."""
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


def cost(raw: Path) -> tuple[list[dict[str, Any]], float | None]:
    """The Cost Management rows, and the warm replica's month from the warm window."""
    rows: list[dict[str, Any]] = [
        row
        for path in sorted((raw / "cost").glob("*.json"))
        if path.name != "window.json"
        for row in json.loads(path.read_text())
    ]
    window = optional_json(raw / "cost" / "window.json")
    if window is None or not rows:
        return rows, None
    warm = [
        float(row["usd"])
        for row in rows
        if window["start"] <= row["date"] <= window["end"]
        and row["resource_type"] == "microsoft.app/containerapps"
    ]
    return rows, round(sum(warm) / window["days"] * 30.4, 2) if warm else None


def optional_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def results(raw: Path) -> dict[str, Any]:
    stream = []
    for run in records(raw / "stream"):
        run = {
            k: v
            for k, v in run.items()
            if k not in {"run", "idle_window_s", "replicas_before"}
        }
        run["attributed_to"] = attributed_to(
            run["duration_s"], run["seconds"], run["ended_by"]
        )
        run["survived"] = survived(run)
        stream.append(run)
    runs = cold_start(raw)
    rows, warm = cost(raw)
    observations = optional_json(raw / "observations.json") or {}
    return {
        "cold_start": runs,
        "cold_start_summary": cold_start_summary(runs),
        "stream": stream,
        "gates": gates(stream),
        "claims": {
            "environment_meter": observations.get(
                "environment_meter", "pending the cost read"
            ),
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
        "projections": projections(optional_json(raw / "rates.json")),
        "cost": rows,
        "warm_replica_usd_per_month": warm,
    }


def main() -> None:
    RESULTS.write_text(json.dumps(results(RAW), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
