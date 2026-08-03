#!/usr/bin/env python3
"""Collect per-node steady-state JSON reports into a reproducible CSV table."""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


def node_count(path: Path) -> int:
    """Extract the node count from a standard mlp-dp-steady-N result path."""
    match = re.search(r"mlp-dp-steady-(\d+)-", str(path))
    if not match:
        raise ValueError(f"cannot determine node count from {path}")
    return int(match.group(1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("result_roots", nargs="+", type=Path)
    args = parser.parse_args()

    rows = []
    for root in args.result_roots:
        report_path = root / "measurement" / "steady_summary.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows.append({"nodes": node_count(root), **report})
    rows.sort(key=lambda row: row["nodes"])
    baseline = next(row for row in rows if row["nodes"] == 1)["steady_wall_seconds"]
    columns = [
        "nodes",
        "steady_wall_seconds",
        "normalized_steady_time",
        "speedup_vs_one_node",
        "cross_node_sync_wall_union_seconds",
        "cross_node_sync_overhead_percent",
        "cross_node_read_events_in_window",
        "cross_node_read_bytes_in_window",
        "coordinator_wall_seconds",
        "boundary_rule",
        "all_pipecomm_events",
    ]
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            row["normalized_steady_time"] = row["steady_wall_seconds"] / baseline
            row["speedup_vs_one_node"] = baseline / row["steady_wall_seconds"]
            writer.writerow({column: row.get(column) for column in columns})


if __name__ == "__main__":
    main()
