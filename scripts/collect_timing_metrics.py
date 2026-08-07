#!/usr/bin/env python3
"""Collect Phase-2 and PipeComm metrics without mixing time domains.

`ns3_phase2_metrics.csv` contains simulated cycles used by LEGOSim's SYNC
protocol.  `pipe-metric` records contain host wall-clock durations of the
functional PipeComm transport.  This collector stores them together only as
separate, explicitly named measurements.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


COUNTERS = ("records", "bytes", "elapsed_ns", "sync_wait_ns", "writes", "reads")


def empty_counter() -> Dict[str, int]:
    return {counter: 0 for counter in COUNTERS}


def add_record(counter: Dict[str, int], record: Dict[str, object]) -> None:
    counter["records"] += 1
    counter["bytes"] += int(record.get("bytes", 0))
    counter["elapsed_ns"] += int(record.get("elapsed_ns", 0))
    counter["sync_wait_ns"] += int(record.get("synchronization_wait_ns", 0))
    counter["writes"] += record.get("operation") == "W"
    counter["reads"] += record.get("operation") == "R"


def interval_union(intervals: Iterable[Tuple[int, int]]) -> int:
    """Return the union duration of wall-clock read-block intervals."""
    ordered = sorted(intervals)
    if not ordered:
        return 0
    total = 0
    start, finish = ordered[0]
    for current_start, current_finish in ordered[1:]:
        if current_start <= finish:
            finish = max(finish, current_finish)
        else:
            total += finish - start
            start, finish = current_start, current_finish
    return total + finish - start


def classify_scope(record: Dict[str, object]) -> str:
    """Read new scope labels and provide a safe interpretation of old logs."""
    scope = record.get("transport_scope")
    if isinstance(scope, str):
        return scope
    if int(record.get("source_slot", -1)) == int(record.get("peer_slot", -2)):
        return "same_logical_worker"
    # Legacy results did not retain physical-host placement.  Their old
    # cross_node flag is only a historical logical-slot classification.
    return "cross_legosim_same_physical_host"


def collect_pipe_metrics(path: Path) -> Dict[str, int]:
    groups = {
        "all": empty_counter(),
        "same_logical_worker": empty_counter(),
        "cross_legosim": empty_counter(),
        "cross_physical_host": empty_counter(),
    }
    intervals: Dict[str, List[Tuple[int, int]]] = {group: [] for group in groups}
    if not path.is_file():
        return {f"{group}_{name}": value for group, values in groups.items() for name, value in values.items()}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("pipe-metric:"):
            continue
        record = json.loads(line.split(":", 1)[1])
        scope = classify_scope(record)
        selected = ["all"]
        if scope == "same_logical_worker":
            selected.append("same_logical_worker")
        else:
            selected.append("cross_legosim")
            if scope == "cross_physical_host":
                selected.append("cross_physical_host")
        for group in selected:
            add_record(groups[group], record)
            if record.get("operation") != "R":
                continue
            started = record.get("started_unix_ns")
            finished = record.get("finished_unix_ns")
            if isinstance(started, int) and isinstance(finished, int) and finished >= started:
                intervals[group].append((started, finished))
    values = {
        f"{group}_{name}": value
        for group, counter in groups.items()
        for name, value in counter.items()
    }
    for group, group_intervals in intervals.items():
        values[f"{group}_sync_wall_union_ns"] = interval_union(group_intervals)
        values[f"{group}_sync_wall_interval_count"] = len(group_intervals)
    return values


def collect_ns3_metrics(path: Optional[Path]) -> Dict[str, int]:
    defaults = {
        "ns3_records": 0,
        "ns3_normal_records": 0,
        "ns3_special_records": 0,
        "ns3_payload_bytes": 0,
        "ns3_normal_source_sync_advance_cycles": 0,
        "ns3_normal_destination_network_delay_cycles": 0,
        "ns3_normal_destination_sync_block_cycles": 0,
    }
    if path is None or not path.is_file():
        return defaults
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            defaults["ns3_records"] += 1
            if row["special"] == "1":
                defaults["ns3_special_records"] += 1
                continue
            defaults["ns3_normal_records"] += 1
            defaults["ns3_payload_bytes"] += int(row["payload_bytes"])
            defaults["ns3_normal_source_sync_advance_cycles"] += int(row["source_sync_advance_cycles"])
            defaults["ns3_normal_destination_network_delay_cycles"] += int(row["destination_network_delay_cycles"])
            defaults["ns3_normal_destination_sync_block_cycles"] += int(row["destination_sync_block_cycles"])
    return defaults


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ns3-metrics", type=Path)
    arguments = parser.parse_args()

    values = collect_pipe_metrics(arguments.transport_log)
    values.update(collect_ns3_metrics(arguments.ns3_metrics))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("w", encoding="utf-8") as handle:
        for key in sorted(values):
            handle.write(f"{key}={values[key]}\n")


if __name__ == "__main__":
    main()
