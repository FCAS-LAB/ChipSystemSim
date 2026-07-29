#!/usr/bin/env python3
"""Aggregate bounded LEGOSim functional-run metrics without mixing in collection time."""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path


METRIC = re.compile(r"pipe-metric: (\{[^\n]+\})")


def mean(values: list[float]) -> float:
    return statistics.fmean(values)


def sample_stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def router_metrics(directory: Path) -> dict[str, float]:
    """Sum router-observed transfer and PipeComm-read waiting metrics."""
    events: list[dict[str, object]] = []
    for log in directory.glob("transport-*.log"):
        for match in METRIC.finditer(log.read_text(encoding="utf-8", errors="replace")):
            events.append(json.loads(match.group(1)))
    cross_writes = [event for event in events if event["cross_node"] and event["operation"] == "W"]
    cross_reads = [event for event in events if event["cross_node"] and event["operation"] == "R"]
    reads = [event for event in events if event["operation"] == "R"]
    return {
        "pipecomm_events": float(len(events)),
        "cross_write_count": float(len(cross_writes)),
        "cross_write_bytes": float(sum(int(event["bytes"]) for event in cross_writes)),
        "cross_write_service_ms": sum(int(event["elapsed_ns"]) for event in cross_writes) / 1_000_000,
        "cross_read_sync_ms": sum(int(event["synchronization_wait_ns"]) for event in cross_reads) / 1_000_000,
        "pipecomm_sync_ms": sum(int(event["synchronization_wait_ns"]) for event in reads) / 1_000_000,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8])
    arguments = parser.parse_args()
    samples: dict[int, list[dict[str, float]]] = {node: [] for node in arguments.nodes}
    for repetition in sorted(arguments.input_root.glob("rep-*")):
        for node in arguments.nodes:
            directory = repetition / f"mlp-nodes{node}"
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if result["status"] != "functional-ok":
                raise RuntimeError(f"{directory} is not functional-ok: {result['status']}")
            sample = {
                "repetition": float(repetition.name.removeprefix("rep-")),
                "measurement_elapsed_seconds": float(result["measurement_elapsed_seconds"]),
                "first_interchiplet_command_seconds": float(result["first_interchiplet_command_seconds"]),
                "all_phase1_started_seconds": float(result["all_phase1_started_seconds"]),
                **router_metrics(directory),
            }
            samples[node].append(sample)
    baseline = mean([sample["measurement_elapsed_seconds"] for sample in samples[1]])
    rows: list[dict[str, float]] = []
    for node, values in samples.items():
        row = {"nodes": float(node), "repetitions": float(len(values))}
        for field in values[0]:
            if field == "repetition":
                continue
            numbers = [sample[field] for sample in values]
            row[f"{field}_mean"] = mean(numbers)
            row[f"{field}_stdev"] = sample_stdev(numbers)
        row["speedup_mean"] = baseline / row["measurement_elapsed_seconds_mean"]
        rows.append(row)
    arguments.output_root.mkdir(parents=True, exist_ok=True)
    (arguments.output_root / "formal_metrics_summary.json").write_text(
        json.dumps({"metric_definitions": {
            "runtime": "deployment through fixed post-startup observation window; excludes log collection and teardown",
            "speedup": "mean one-node runtime divided by mean runtime at this node count",
            "cross_write_service": "router-observed time to submit each cross-node PipeComm write through its local BaseIf gateway",
            "pipecomm_sync": "sum of router-observed blocking time for all PipeComm reads",
        }, "samples": samples, "summary": rows}, indent=2), encoding="utf-8"
    )
    fields = list(rows[0])
    with (arguments.output_root / "formal_metrics_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
