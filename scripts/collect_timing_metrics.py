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
TRANSPORT_COUNTERS = (
    "transport_pairs",
    "transport_unmatched_writes",
    "transport_unmatched_reads",
    "transport_exposed_sync_wall_sum_ns",
    "transport_exposed_sync_wall_union_ns",
)


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


def selected_groups(scope: str) -> List[str]:
    """Return every aggregation scope to which a transport record belongs."""
    groups = ["all"]
    if scope == "same_logical_worker":
        groups.append("same_logical_worker")
    else:
        groups.append("cross_legosim")
        if scope == "cross_physical_host":
            groups.append("cross_physical_host")
    return groups


def transfer_key(record: Dict[str, object]) -> Optional[Tuple[int, int, str, int]]:
    """Identify one FIFO item by its producer and consumer worker slots.

    Legacy logs have no pipe name and are deliberately excluded: guessing a
    correspondence from byte count alone could turn compute waiting into a
    seemingly precise communication metric.
    """
    pipe_name = record.get("pipe_name")
    if not isinstance(pipe_name, str) or not pipe_name:
        return None
    try:
        source_slot = int(record["source_slot"])
        peer_slot = int(record["peer_slot"])
        byte_count = int(record["bytes"])
    except (KeyError, TypeError, ValueError):
        return None
    if record.get("operation") == "W":
        return source_slot, peer_slot, pipe_name, byte_count
    if record.get("operation") == "R":
        return peer_slot, source_slot, pipe_name, byte_count
    return None


def collect_exposed_transport_metrics(records: Iterable[Dict[str, object]]) -> Dict[str, int]:
    """Measure transfer-only READ blocking, excluding both endpoints' compute.

    A consumer READ may start before the producer has computed and submitted
    its message. Its raw elapsed time therefore includes producer compute. For
    a matched FIFO item, only the interval from ``max(write_started,
    read_started)`` to ``read_finished`` is counted. It begins once both the
    payload submission and the consumer are ready, so it contains transport
    and protocol progress but neither endpoint's pre-operation computation.
    """
    groups = ("all", "same_logical_worker", "cross_legosim", "cross_physical_host")
    values = {f"{group}_{counter}": 0 for group in groups for counter in TRANSPORT_COUNTERS}
    writes: Dict[Tuple[int, int, str, int], List[Dict[str, object]]] = {}
    reads: Dict[Tuple[int, int, str, int], List[Dict[str, object]]] = {}
    for record in records:
        key = transfer_key(record)
        if key is None:
            continue
        if record.get("operation") == "W":
            writes.setdefault(key, []).append(record)
        elif record.get("operation") == "R":
            reads.setdefault(key, []).append(record)

    intervals: Dict[str, List[Tuple[int, int]]] = {group: [] for group in groups}
    for key in sorted(set(writes) | set(reads)):
        write_items = sorted(writes.get(key, []), key=lambda item: int(item.get("started_unix_ns", -1)))
        read_items = sorted(reads.get(key, []), key=lambda item: int(item.get("finished_unix_ns", -1)))
        matched_writes = 0
        matched_reads = 0
        for write, read in zip(write_items, read_items):
            try:
                write_started = int(write["started_unix_ns"])
                read_started = int(read["started_unix_ns"])
                read_finished = int(read["finished_unix_ns"])
            except (KeyError, TypeError, ValueError):
                continue
            interval_started = max(write_started, read_started)
            if read_finished < interval_started:
                continue
            scope = classify_scope(write)
            for group in selected_groups(scope):
                values[f"{group}_transport_pairs"] += 1
                values[f"{group}_transport_exposed_sync_wall_sum_ns"] += read_finished - interval_started
                intervals[group].append((interval_started, read_finished))
            matched_writes += 1
            matched_reads += 1

        scope_source = write_items[0] if write_items else (read_items[0] if read_items else None)
        if scope_source is not None:
            for group in selected_groups(classify_scope(scope_source)):
                values[f"{group}_transport_unmatched_writes"] += len(write_items) - matched_writes
                values[f"{group}_transport_unmatched_reads"] += len(read_items) - matched_reads

    for group, group_intervals in intervals.items():
        values[f"{group}_transport_exposed_sync_wall_union_ns"] = interval_union(group_intervals)
    return values


def collect_pipe_metrics(path: Path) -> Dict[str, int]:
    groups = {
        "all": empty_counter(),
        "same_logical_worker": empty_counter(),
        "cross_legosim": empty_counter(),
        "cross_physical_host": empty_counter(),
    }
    intervals: Dict[str, List[Tuple[int, int]]] = {group: [] for group in groups}
    records: List[Dict[str, object]] = []
    defaults = {
        f"{group}_{name}": value
        for group, counter in groups.items()
        for name, value in counter.items()
    }
    defaults.update(collect_exposed_transport_metrics(()))
    if not path.is_file():
        return defaults
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("pipe-metric:"):
            continue
        record = json.loads(line.split(":", 1)[1])
        records.append(record)
        scope = classify_scope(record)
        for group in selected_groups(scope):
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
    values.update(collect_exposed_transport_metrics(records))
    return values


def collect_ns3_metrics(path: Optional[Path]) -> Dict[str, int]:
    defaults = {
        "ns3_records": 0,
        "ns3_normal_records": 0,
        "ns3_special_records": 0,
        "ns3_payload_bytes": 0,
        "ns3_normal_payload_bytes": 0,
        "ns3_normal_source_sync_advance_cycles": 0,
        "ns3_normal_destination_network_delay_cycles": 0,
        "ns3_normal_destination_sync_block_cycles": 0,
    }
    if path is None or not path.is_file():
        return defaults
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            defaults["ns3_records"] += 1
            payload_bytes = int(row["payload_bytes"])
            # Keep this total aligned with ns3_phase2.cc's JSON summary.  The
            # special records are control operations, so their delay values
            # are deliberately not included in the normal READ/WRITE totals.
            defaults["ns3_payload_bytes"] += payload_bytes
            if row["special"] == "1":
                defaults["ns3_special_records"] += 1
                continue
            defaults["ns3_normal_records"] += 1
            defaults["ns3_normal_payload_bytes"] += payload_bytes
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
