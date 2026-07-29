#!/usr/bin/env python3
"""Build and execute the eight-workload 1/2/4/8-node Docker matrix."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

# Keep the project root lexical rather than resolving symlinks. This avoids
# changing the path a Docker CLI uses when repositories are bind-mounted.
ROOT = Path(os.path.abspath(Path(__file__).parent.parent))
RESULTS = ROOT / "results"

# Reduced-input synthetic traces. Their names and workload families follow
# Table II of ODHS_HPCA_v4(1).pdf; they are not the paper's multi-terabyte
# datasets. Every profile runs on a fixed four-CPU, eight-GPU component set.
WORKLOADS = {
    "mlp": {"label": "MLP", "rounds": 6, "input_messages_per_round": 2, "payload_bytes": 262144, "result_bytes": 32768, "cpu_compute_ms": 8, "gpu_compute_ms": 35},
    "dlrm": {"label": "DLRM", "rounds": 8, "input_messages_per_round": 3, "payload_bytes": 65536, "result_bytes": 16384, "cpu_compute_ms": 5, "gpu_compute_ms": 22},
    "resnet50": {"label": "ResNet-50", "rounds": 5, "input_messages_per_round": 2, "payload_bytes": 393216, "result_bytes": 65536, "cpu_compute_ms": 7, "gpu_compute_ms": 30},
    "mixtral8x7b": {"label": "Mixtral-8x7B", "rounds": 10, "input_messages_per_round": 2, "payload_bytes": 524288, "result_bytes": 131072, "cpu_compute_ms": 5, "gpu_compute_ms": 18},
    "bfs": {"label": "BFS", "rounds": 12, "input_messages_per_round": 2, "payload_bytes": 24576, "result_bytes": 8192, "cpu_compute_ms": 2, "gpu_compute_ms": 6},
    "fft": {"label": "FFT", "rounds": 8, "input_messages_per_round": 2, "payload_bytes": 131072, "result_bytes": 32768, "cpu_compute_ms": 3, "gpu_compute_ms": 12},
    "pagerank": {"label": "PageRank", "rounds": 10, "input_messages_per_round": 2, "payload_bytes": 32768, "result_bytes": 16384, "cpu_compute_ms": 2, "gpu_compute_ms": 8},
    "pde": {"label": "PDE", "rounds": 7, "input_messages_per_round": 2, "payload_bytes": 98304, "result_bytes": 24576, "cpu_compute_ms": 4, "gpu_compute_ms": 16},
}


def placement_for(container_count: int) -> dict[str, list[str]]:
    """Place the same four CPU and eight GPU simlets on N Docker nodes."""
    placements = {
        1: {"0": ["cpu0", "cpu1", "cpu2", "cpu3", *[f"gpu{i}" for i in range(8)], "ns3"]},
        2: {
            "0": ["cpu0", "cpu1", "gpu0", "gpu2", "gpu4", "gpu6"],
            "1": ["cpu2", "cpu3", "gpu1", "gpu3", "gpu5", "gpu7", "ns3"],
        },
        4: {
            "0": ["cpu0", "gpu0", "gpu3"],
            "1": ["cpu1", "gpu2", "gpu5"],
            "2": ["cpu2", "gpu4", "gpu7"],
            "3": ["cpu3", "gpu6", "gpu1", "ns3"],
        },
        8: {
            "0": ["cpu0", "gpu0"], "1": ["cpu1", "gpu2"], "2": ["cpu2", "gpu4"], "3": ["cpu3", "gpu6"],
            "4": ["gpu1"], "5": ["gpu3"], "6": ["gpu5"], "7": ["gpu7", "ns3"],
        },
    }
    return placements[container_count]


def write_run_files(benchmark: str, container_count: int, repetition: int) -> tuple[Path, Path]:
    run_id = f"{benchmark}-nodes{container_count}-rep{repetition}"
    run_dir = RESULTS / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    placement = placement_for(container_count)
    config = {
        "run_id": run_id,
        "benchmark": WORKLOADS[benchmark]["label"],
        "container_count": container_count,
        "placement": placement,
        "proxies": [],
        "output_dir": "/run",
        "network": {"bandwidth_mbps": 1000, "propagation_us": 10},
        "workload": WORKLOADS[benchmark],
        "source_workload": "ODHS Table II reduced-input synthetic trace",
    }
    (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    services = {
        f"node{node_id}": {
            "image": "simbricks-legosim:latest",
            "command": ["--node-id", str(node_id)],
            "environment": {"SIM_CONFIG": json.dumps(config, separators=(",", ":"))},
            "networks": ["simulation"],
        }
        for node_id in placement
    }
    compose = {"services": services, "networks": {"simulation": {"driver": "bridge"}}}
    compose_path = run_dir / "compose.json"
    compose_path.write_text(json.dumps(compose, indent=2), encoding="utf-8")
    return run_dir, compose_path


def run_command(command: list[str]) -> None:
    print("+", shlex.join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def collect_measurement(log_output: str, benchmark: str, container_count: int, repetition: int) -> dict[str, float]:
    marker = "MEASUREMENT_JSON="
    encoded = next((line.split(marker, 1)[1] for line in log_output.splitlines() if marker in line), None)
    if encoded is None:
        raise RuntimeError("CPU container did not emit a measurement record")
    result = json.loads(encoded)
    return {
        "benchmark": result["benchmark"],
        "containers": container_count,
        "repetition": repetition,
        "total_time_s": result["total_time_ns"] / 1e9,
        "synchronization_time_s": result["synchronization_time_ns"] / 1e9,
        "cross_machine_time_s": result["cross_machine_time_ns"] / 1e9,
        "synchronization_fraction": result["synchronization_fraction"],
    }


def write_summary(rows: list[dict[str, float]]) -> None:
    baselines = {
        benchmark: statistics.mean(
            row["total_time_s"]
            for row in rows
            if row["benchmark"] == benchmark and row["containers"] == 1
        )
        for benchmark in {row["benchmark"] for row in rows}
    }
    for row in rows:
        row["parallel_efficiency"] = baselines[row["benchmark"]] / (row["total_time_s"] * row["containers"])
    with (RESULTS / "summary.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    analyze = ROOT / "scripts" / "analyze_results.py"
    run_command([sys.executable, str(analyze), str(RESULTS / "summary.csv")])


def load_existing_rows() -> list[dict[str, float]]:
    """Load prior benchmark groups when collecting the matrix in batches."""
    summary_path = RESULTS / "summary.csv"
    if not summary_path.exists():
        return []
    with summary_path.open(encoding="utf-8", newline="") as source:
        rows: list[dict[str, float]] = []
        for row in csv.DictReader(source):
            rows.append({
                "benchmark": row["benchmark"],
                "containers": int(row["containers"]),
                "repetition": int(row["repetition"]),
                "total_time_s": float(row["total_time_s"]),
                "synchronization_time_s": float(row["synchronization_time_s"]),
                "cross_machine_time_s": float(row["cross_machine_time_s"]),
                "synchronization_fraction": float(row["synchronization_fraction"]),
                "parallel_efficiency": float(row["parallel_efficiency"]),
            })
    return rows


def main() -> None:
    global RESULTS
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--benchmarks", nargs="+", choices=sorted(WORKLOADS), default=sorted(WORKLOADS))
    parser.add_argument("--keep-runs", action="store_true")
    parser.add_argument("--append", action="store_true", help="Preserve and append existing summary.csv rows.")
    parser.add_argument("--skip-build", action="store_true", help="Use an already-built simbricks-legosim:latest image.")
    parser.add_argument("--output-root", type=Path,
                        help="write runs, summary.csv, and REPORT.md to this separate directory")
    arguments = parser.parse_args()
    if arguments.repetitions < 1:
        raise ValueError("--repetitions must be at least one")
    if arguments.output_root is not None:
        RESULTS = arguments.output_root.resolve()
    RESULTS.mkdir(exist_ok=True)
    if not arguments.keep_runs and not arguments.append:
        shutil.rmtree(RESULTS / "runs", ignore_errors=True)
    if arguments.skip_build:
        run_command(["docker", "image", "inspect", "simbricks-legosim:latest"])
    else:
        run_command(["docker", "build", "--tag", "simbricks-legosim:latest", "--file", "docker/Dockerfile", "."])
    rows = load_existing_rows() if arguments.append else []
    for benchmark in arguments.benchmarks:
        for container_count in (1, 2, 4, 8):
            for repetition in range(1, arguments.repetitions + 1):
                run_dir, compose_path = write_run_files(benchmark, container_count, repetition)
                project = f"sl{benchmark[:5]}{container_count}r{repetition}"
                compose_file = os.path.abspath(compose_path)
                command = ["docker", "compose", "--project-name", project, "--file", compose_file, "up", "--abort-on-container-exit", "--exit-code-from", "node0"]
                try:
                    print("+", shlex.join(command), flush=True)
                    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
                    sys.stdout.write(completed.stdout)
                    sys.stderr.write(completed.stderr)
                    completed.check_returncode()
                finally:
                    run_command(["docker", "compose", "--project-name", project, "--file", compose_file, "down", "--volumes", "--timeout", "1"])
                rows.append(collect_measurement(completed.stdout + completed.stderr, benchmark, container_count, repetition))
    write_summary(rows)


if __name__ == "__main__":
    main()
