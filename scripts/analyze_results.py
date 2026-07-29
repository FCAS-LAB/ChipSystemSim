#!/usr/bin/env python3
"""Generate the eight-workload, 1/2/4/8 Docker-node result report."""
from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def average(rows: list[dict[str, float]], column: str) -> float:
    return statistics.mean(row[column] for row in rows)


def main() -> None:
    summary_path = Path(sys.argv[1])
    groups: dict[tuple[str, int], list[dict[str, float]]] = defaultdict(list)
    with summary_path.open(encoding="utf-8") as source:
        for source_row in csv.DictReader(source):
            benchmark = source_row.pop("benchmark")
            node_count = int(source_row.pop("containers"))
            source_row.pop("repetition")
            groups[(benchmark, node_count)].append(
                {name: float(value) for name, value in source_row.items()}
            )

    benchmarks = sorted({benchmark for benchmark, _ in groups})
    repetitions = len(next(iter(groups.values())))
    lines = [
        "# Eight-workload Docker-node experiment results",
        "",
        "The workload names follow ODHS Table II. Inputs are reduced synthetic traces, not the paper's original large datasets.",
        f"Each entry is the mean of {repetitions} independent Docker run(s) on this one host.",
        "Every configuration contains the same four CPU simlets and eight GPU simlets; only their Docker-node placement changes.",
        "",
        "| Benchmark | Nodes | Total time (s) | Cross-node communication (s) | Synchronization time (s) | Parallel efficiency |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for benchmark in benchmarks:
        baseline = average(groups[(benchmark, 1)], "total_time_s")
        for node_count in (1, 2, 4, 8):
            rows = groups[(benchmark, node_count)]
            total_time = average(rows, "total_time_s")
            lines.append(
                "| {benchmark} | {nodes} | {total:.6f} | {cross:.6f} | {sync:.6f} | {efficiency:.3f} |".format(
                    benchmark=benchmark,
                    nodes=node_count,
                    total=total_time,
                    cross=average(rows, "cross_machine_time_s"),
                    sync=average(rows, "synchronization_time_s"),
                    efficiency=baseline / (total_time * node_count),
                )
            )
    lines.extend([
        "",
        "## Metric definitions",
        "",
        "- Total time: CPU0 controller wall-clock interval from trace start to all four CPU workflows completing.",
        "- Cross-node communication: critical-path time consumed by application data transfers whose source and destination are in different Docker nodes; it includes ns-3-modeled link delay and RPC handling.",
        "- Synchronization time: critical-path controller wait for data-transfer completion and GPU replies.",
        "- Parallel efficiency: T1 / (Tn * n), separately for each benchmark.",
        "- Node mapping: 1 node co-locates all 12 simlets and ns-3. The 2-, 4-, and 8-node placements distribute the same simlets without proxy-only nodes; each multi-node placement has cross-node CPU-to-GPU traffic.",
    ])
    summary_path.with_name("REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
