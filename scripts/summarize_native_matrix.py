#!/usr/bin/env python3
"""Summarize one bounded native LEGOSim run for every workload/node pair.

The tool intentionally keeps the measurement interval separate from log
collection and Swarm cleanup.  Router values are sums over the captured log
tail, so they are transport observations rather than end-to-end application
completion times.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


METRIC = re.compile(r"pipe-metric: (\{[^\n]+\})")


def transport_totals(directory: Path) -> dict[str, float]:
    """Return router-observed PipeComm totals from every transport log."""
    events: list[dict[str, object]] = []
    for log in directory.glob("transport-*.log"):
        for match in METRIC.finditer(log.read_text(encoding="utf-8", errors="replace")):
            events.append(json.loads(match.group(1)))

    cross_writes = [item for item in events if item["cross_node"] and item["operation"] == "W"]
    cross_reads = [item for item in events if item["cross_node"] and item["operation"] == "R"]
    reads = [item for item in events if item["operation"] == "R"]
    return {
        "pipecomm_events": len(events),
        "cross_write_count": len(cross_writes),
        "cross_write_bytes": sum(int(item["bytes"]) for item in cross_writes),
        "cross_write_service_ms": round(sum(int(item["elapsed_ns"]) for item in cross_writes) / 1_000_000, 6),
        "cross_read_sync_ms": round(sum(int(item["synchronization_wait_ns"]) for item in cross_reads) / 1_000_000, 6),
        "pipecomm_sync_ms": round(sum(int(item["synchronization_wait_ns"]) for item in reads) / 1_000_000, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--workloads", nargs="+", required=True)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8])
    arguments = parser.parse_args()

    rows: list[dict[str, object]] = []
    for workload in arguments.workloads:
        workload_rows: list[dict[str, object]] = []
        for node_count in arguments.nodes:
            directory = arguments.input_root / f"{workload}-nodes{node_count}"
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if result["status"] != "functional-ok":
                raise RuntimeError(f"{directory} status is {result['status']}")
            row = {
                "workload": workload,
                "nodes": node_count,
                "status": result["status"],
                "measurement_elapsed_seconds": result["measurement_elapsed_seconds"],
                "first_interchiplet_command_seconds": result["first_interchiplet_command_seconds"],
                "all_phase1_started_seconds": result["all_phase1_started_seconds"],
                **transport_totals(directory),
            }
            workload_rows.append(row)
        baseline = float(workload_rows[0]["measurement_elapsed_seconds"])
        for row in workload_rows:
            row["speedup_vs_1node"] = round(baseline / float(row["measurement_elapsed_seconds"]), 6)
            rows.append(row)

    csv_path = arguments.input_root / "native_matrix_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    json_path = arguments.input_root / "native_matrix_summary.json"
    json_path.write_text(json.dumps({
        "metric_definitions": {
            "measurement_elapsed_seconds": "deployment through the fixed 120-second post-startup observation; excludes log collection and teardown",
            "speedup_vs_1node": "same-workload one-node measurement elapsed time divided by this point's elapsed time",
            "cross_write_service_ms": "sum of local router service time for captured cross-node PipeComm writes",
            "pipecomm_sync_ms": "sum of captured router blocking time for PipeComm reads",
        },
        "rows": rows,
    }, indent=2), encoding="utf-8")
    print(f"wrote {len(rows)} rows to {csv_path}")


if __name__ == "__main__":
    main()
