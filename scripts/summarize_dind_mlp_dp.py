#!/usr/bin/env python3
"""Summarize natural-completion MLP-DP DinD runs with wall-clock sync time."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict


def parse_timestamp(value: str) -> float:
    """Parse Docker's nanosecond UTC timestamp on Python 3.6 and newer."""
    date_part, fraction = value.rstrip("Z").split(".", 1)
    base = datetime.strptime(date_part, "%Y-%m-%dT%H:%M:%S").timestamp()
    return base + float("0." + fraction)


def read_key_values(path: Path) -> Dict[str, str]:
    """Read the simple key=value artifacts emitted by run_dind_mlp_dp.sh."""
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"invalid key=value line in {path}: {line!r}")
        values[key] = value
    return values


def collect_run(run_directory: Path, nodes: int) -> Dict[str, object]:
    """Validate one run and return reportable wall-clock metrics."""
    timing = read_key_values(run_directory / "coordinator_timing.txt")
    metrics = read_key_values(run_directory / "metrics.txt")
    coordinator_log = (run_directory / "coordinator.log").read_text(
        encoding="utf-8", errors="replace"
    )
    if timing.get("exit") != "0":
        raise RuntimeError(f"{run_directory}: coordinator exit is not zero")
    if "End of Simulation" not in coordinator_log:
        raise RuntimeError(f"{run_directory}: natural-completion marker is missing")

    total_seconds = parse_timestamp(timing["finish"]) - parse_timestamp(timing["start"])
    if total_seconds <= 0:
        raise RuntimeError(f"{run_directory}: non-positive coordinator duration")
    sync_seconds = int(metrics.get("cross_sync_wall_union_ns", "0")) / 1_000_000_000
    return {
        "nodes": nodes,
        "total_simulation_seconds": total_seconds,
        "cross_node_sync_wall_seconds": sync_seconds,
        "cross_node_sync_overhead_percent": 100.0 * sync_seconds / total_seconds,
        "cross_node_bytes": int(metrics["cross_bytes"]),
        "cross_node_records": int(metrics["cross_records"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize 1/2/4/8-node MLP-DP DinD run directories."
    )
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8],
                        choices=[1, 2, 4, 8])
    parser.add_argument("--directory-pattern", default="nodes{nodes}",
                        help="subdirectory pattern below input root (default: nodes{nodes})")
    arguments = parser.parse_args()

    rows = []
    for nodes in arguments.nodes:
        directory = arguments.input_root / arguments.directory_pattern.format(nodes=nodes)
        rows.append(collect_run(directory, nodes))

    baseline = rows[0]["total_simulation_seconds"]
    for row in rows:
        row["speedup_vs_first_row"] = baseline / row["total_simulation_seconds"]

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with arguments.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
