"""Measures one streamed response: when each byte group arrived and how the body ended.

    python -m dispute_casework.topology_smoke --url-env PUBLIC_URL --leg cloudflare

One JSON record per run, on stdout and, given --out, in a file. The URL comes
from the named environment variable and the header value from ORIGIN_HEADER_SECRET.
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from dispute_casework.api import ORIGIN_HEADER

ENDED_BY: dict[type[httpx.HTTPError], str] = {
    httpx.ReadTimeout: "timeout",
    httpx.RemoteProtocolError: "reset",
    httpx.ReadError: "reset",
    httpx.ConnectError: "connect",
}


def measure(url: str, secret: str, seconds: int, silent: bool) -> dict[str, Any]:
    params = {"seconds": seconds, "silent": "true" if silent else "false"}
    # The silent arm sends nothing for the whole run; the client waits longer.
    timeout = httpx.Timeout(30, read=seconds + 60)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    start = time.monotonic()
    status = None
    t_headers = None
    chunks: list[dict[str, float]] = []
    ended_by = "server"
    try:
        with (
            httpx.Client(timeout=timeout) as client,
            client.stream(
                "GET", f"{url}/stream", params=params, headers={ORIGIN_HEADER: secret}
            ) as response,
        ):
            status = response.status_code
            t_headers = time.monotonic() - start
            if status != 200:
                ended_by = "status"
            else:
                for chunk in response.iter_raw():
                    elapsed = time.monotonic() - start
                    chunks.append({"t": round(elapsed, 3), "bytes": len(chunk)})
    except httpx.HTTPError as error:
        ended_by = ENDED_BY.get(type(error), f"error:{type(error).__name__}")
    duration = time.monotonic() - start
    times = [t for t in (t_headers, *(chunk["t"] for chunk in chunks)) if t is not None]
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    return {
        "arm": "silent" if silent else "heartbeat",
        "seconds": seconds,
        "started_at": started_at,
        "status": status,
        "t_headers_s": None if t_headers is None else round(t_headers, 3),
        "t_first_chunk_s": chunks[0]["t"] if chunks else None,
        "duration_s": round(duration, 3),
        "bytes": sum(int(chunk["bytes"]) for chunk in chunks),
        "chunks": chunks,
        "max_gap_s": round(max(gaps), 3) if gaps else None,
        "ended_by": ended_by,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-env", default="INTERNAL_URL")
    parser.add_argument("--leg", default="internal")
    parser.add_argument(
        "--series", help="a series outside the pre-registration, kept apart"
    )
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--run", type=int)
    parser.add_argument("--idle-window-s", type=int)
    parser.add_argument("--replicas-before", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    record = {
        "leg": args.leg,
        "series": args.series,
        "url_env": args.url_env,
        "run": args.run,
        "idle_window_s": args.idle_window_s,
        "replicas_before": args.replicas_before,
        **measure(
            os.environ[args.url_env],
            os.environ["ORIGIN_HEADER_SECRET"],
            args.seconds,
            args.silent,
        ),
    }
    text = json.dumps(record, indent=2) + "\n"
    sys.stdout.write(text)
    if args.out:
        args.out.write_text(text)


if __name__ == "__main__":
    main()
