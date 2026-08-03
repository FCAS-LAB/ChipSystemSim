#!/usr/bin/env python3
"""Summarize one MLP-DP steady-state measurement from PipeComm event logs.

The coordinator duration includes DinD service startup, worker registration,
and worker shutdown.  This tool instead brackets application work using
PipeComm events:

* multi-rank runs: rank 0's final start-barrier release through the final
  non-zero rank completion-barrier write;
* single-rank runs: first work-header write through final payload write.

The second rule is necessarily an approximation because a one-rank run has no
cross-rank barrier.  The output explicitly records which rule was used.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def parse_records(path: Path) -> list[dict[str, Any]]:
    """Return all JSON objects emitted by the PipeComm transport logger."""
    records: list[dict[str, Any]] = []
    prefix = "pipe-metric: "
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if prefix not in line:
            continue
        _, payload = line.split(prefix, 1)
        try:
            records.append(json.loads(payload))
        except json.JSONDecodeError:
            # A malformed diagnostic line must not invalidate a completed run.
            continue
    if not records:
        raise ValueError(f"no PipeComm metric records found in {path}")
    return records


def event_time(record: dict[str, Any], name: str) -> int:
    value = record.get(name)
    if not isinstance(value, int):
        raise ValueError(f"metric record has no integer {name}: {record}")
    return value


def union_duration(intervals: Iterable[tuple[int, int]]) -> int:
    """Compute the duration of the union of non-empty nanosecond intervals."""
    merged: list[list[int]] = []
    for start, finish in sorted(intervals):
        if finish <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, finish])
        else:
            merged[-1][1] = max(merged[-1][1], finish)
    return sum(finish - start for start, finish in merged)


def choose_window(records: list[dict[str, Any]]) -> tuple[int, int, str]:
    """Find the stable MLP interval and state the selected measurement rule."""
    start_releases = [
        record
        for record in records
        if record.get("operation") == "W"
        and record.get("bytes") == 4
        and record.get("source_slot") == 0
        and record.get("peer_slot") != 0
    ]
    completion_writes = [
        record
        for record in records
        if record.get("operation") == "W"
        and record.get("bytes") == 4
        and record.get("source_slot") != 0
        and record.get("peer_slot") == 0
    ]
    if start_releases and completion_writes:
        # The first release fan-out is the initial barrier.  Some deployed
        # LEGOSim images include later 4-byte control fan-outs, so selecting
        # the last 0-to-peer event would incorrectly shorten the MLP window.
        # One release is written to every non-zero rank.
        releases_by_time = sorted(start_releases, key=lambda record: event_time(record, "finished_unix_ns"))
        peer_count = len({int(record["peer_slot"]) for record in releases_by_time})
        initial_releases = releases_by_time[:peer_count]
        start = max(event_time(record, "finished_unix_ns") for record in initial_releases)
        finish = max(event_time(record, "finished_unix_ns") for record in completion_writes)
        return start, finish, "multi_rank_barriers"

    headers = [
        record
        for record in records
        if record.get("operation") == "W" and record.get("bytes") == 16
    ]
    payload_writes = [
        record
        for record in records
        if record.get("operation") == "W" and isinstance(record.get("bytes"), int)
        and record["bytes"] > 16
    ]
    if not headers or not payload_writes:
        raise ValueError("single-rank measurement lacks work-header or payload events")
    start = min(event_time(record, "started_unix_ns") for record in headers)
    finish = max(event_time(record, "finished_unix_ns") for record in payload_writes)
    return start, finish, "single_rank_first_header_to_final_payload"


def parse_rfc3339_timestamp(value: str) -> datetime:
    """Parse coordinator timestamps, including their nanosecond fraction."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1]
        timezone = "+00:00"
    else:
        timezone = ""
    if "." in normalized:
        seconds, fraction = normalized.split(".", 1)
        # datetime retains microseconds; PipeComm supplies the nanosecond data
        # used for the primary metric, so truncation affects only this reference.
        normalized = f"{seconds}.{fraction[:6].ljust(6, '0')}"
    return datetime.fromisoformat(normalized + timezone)


def coordinator_seconds(path: Path | None) -> float | None:
    """Read elapsed coordinator wall time when the optional timing file exists."""
    if path is None or not path.is_file():
        return None
    values = dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    if "start" not in values or "finish" not in values:
        return None
    start = parse_rfc3339_timestamp(values["start"])
    finish = parse_rfc3339_timestamp(values["finish"])
    return (finish - start).total_seconds()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transport_log", type=Path)
    parser.add_argument("--coordinator-timing", type=Path)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    records = parse_records(args.transport_log)
    start, finish, boundary_rule = choose_window(records)
    if finish <= start:
        raise ValueError("selected steady-state window is non-positive")

    cross_intervals: list[tuple[int, int]] = []
    cross_records = 0
    cross_bytes = 0
    for record in records:
        if not record.get("cross_node") or record.get("operation") != "R":
            continue
        record_start = event_time(record, "started_unix_ns")
        record_finish = event_time(record, "finished_unix_ns")
        clipped_start = max(start, record_start)
        clipped_finish = min(finish, record_finish)
        if clipped_finish <= clipped_start:
            continue
        cross_intervals.append((clipped_start, clipped_finish))
        cross_records += 1
        cross_bytes += int(record.get("bytes", 0))

    steady_ns = finish - start
    cross_sync_ns = union_duration(cross_intervals)
    report = {
        "metric_definition": "PipeComm-delimited external steady-state wall time",
        "boundary_rule": boundary_rule,
        "steady_start_unix_ns": start,
        "steady_finish_unix_ns": finish,
        "steady_wall_seconds": steady_ns / 1e9,
        "cross_node_sync_wall_union_seconds": cross_sync_ns / 1e9,
        "cross_node_sync_overhead_percent": 100.0 * cross_sync_ns / steady_ns,
        "cross_node_read_events_in_window": cross_records,
        "cross_node_read_bytes_in_window": cross_bytes,
        "coordinator_wall_seconds": coordinator_seconds(args.coordinator_timing),
        "all_pipecomm_events": len(records),
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
