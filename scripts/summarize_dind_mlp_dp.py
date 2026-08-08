#!/usr/bin/env python3
"""Summarize natural-completion LEGOSim DinD runs without mixing time domains."""
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
    logical_sync_seconds = int(metrics.get("cross_legosim_sync_wall_union_ns", "0")) / 1_000_000_000
    physical_sync_seconds = int(metrics.get("cross_physical_host_sync_wall_union_ns", "0")) / 1_000_000_000
    return {
        "nodes": nodes,
        "total_simulation_seconds": total_seconds,
        # Different logical workers in a one-host DinD experiment are
        # cross-LEGOSim, not cross-physical-machine. Keep both measurements
        # explicit so a chart cannot silently promote a container boundary to
        # a physical network boundary.
        "cross_legosim_sync_wall_seconds": logical_sync_seconds,
        "cross_legosim_sync_overhead_percent": 100.0 * logical_sync_seconds / total_seconds,
        "cross_legosim_bytes": int(metrics.get("cross_legosim_bytes", "0")),
        "cross_legosim_records": int(metrics.get("cross_legosim_records", "0")),
        "cross_physical_host_sync_wall_seconds": physical_sync_seconds,
        "cross_physical_host_sync_overhead_percent": 100.0 * physical_sync_seconds / total_seconds,
        "cross_physical_host_bytes": int(metrics.get("cross_physical_host_bytes", "0")),
        "cross_physical_host_records": int(metrics.get("cross_physical_host_records", "0")),
        "ns3_normal_records": int(metrics.get("ns3_normal_records", "0")),
        "ns3_normal_source_sync_advance_cycles": int(
            metrics.get("ns3_normal_source_sync_advance_cycles", "0")
        ),
        "ns3_normal_destination_network_delay_cycles": int(
            metrics.get("ns3_normal_destination_network_delay_cycles", "0")
        ),
        "ns3_normal_destination_sync_block_cycles": int(
            metrics.get("ns3_normal_destination_sync_block_cycles", "0")
        ),
    }


def parse_explicit_run(value: str) -> tuple[int, Path]:
    """Parse ``NODES=RESULT_DIRECTORY`` and reject ambiguous input early."""
    node_text, separator, directory_text = value.partition("=")
    if not separator or not node_text or not directory_text:
        raise argparse.ArgumentTypeError(
            "--run must use NODES=RESULT_DIRECTORY, for example 4=/data/nodes4"
        )
    try:
        nodes = int(node_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid node count in --run: {node_text!r}") from error
    if nodes <= 0:
        raise argparse.ArgumentTypeError("--run node count must be positive")
    return nodes, Path(directory_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize natural-completion LEGOSim DinD run directories."
    )
    parser.add_argument("--input-root", type=Path,
                        help="root used with --directory-pattern (legacy matrix layout)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8],
                        choices=[1, 2, 4, 8])
    parser.add_argument("--directory-pattern", default="nodes{nodes}",
                        help="subdirectory pattern below input root (default: nodes{nodes})")
    parser.add_argument("--run", action="append", type=parse_explicit_run,
                        metavar="NODES=RESULT_DIRECTORY",
                        help="explicit result directory; repeat for differently named runs")
    arguments = parser.parse_args()

    if arguments.run and arguments.input_root:
        parser.error("use either --run or --input-root/--directory-pattern, not both")
    if arguments.run:
        explicit_runs = arguments.run
        node_counts = [nodes for nodes, _ in explicit_runs]
        if len(set(node_counts)) != len(node_counts):
            parser.error("each --run node count may be specified only once")
        rows = [collect_run(directory, nodes) for nodes, directory in explicit_runs]
    else:
        if arguments.input_root is None:
            parser.error("provide --run or --input-root")
        rows = []
        for nodes in arguments.nodes:
            directory = arguments.input_root / arguments.directory_pattern.format(nodes=nodes)
            rows.append(collect_run(directory, nodes))

    rows.sort(key=lambda row: int(row["nodes"]))
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
