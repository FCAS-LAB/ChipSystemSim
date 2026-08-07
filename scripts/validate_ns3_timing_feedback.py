#!/usr/bin/env python3
"""Prove that an ns-3 Phase-2 result was consumed by the next Phase-1 round."""
from __future__ import print_function

import argparse
import csv
import json
import re
import sys
from pathlib import Path


ROUND_DIRECTORY = re.compile(r"^proc_r([0-9]+)_p2_t([0-9]+)$")
ROUND_START = re.compile(r"\*{4} Round ([0-9]+) Phase 1")
LOADED_DELAY = re.compile(r"Load ([0-9]+) delay records\.")


def line_count(path):
    """Return non-empty records; the protocol intentionally has no header."""
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def phase_two_directories(artifact_root):
    """Return Process directories in numeric round/thread order."""
    found = []
    for directory in artifact_root.iterdir():
        if not directory.is_dir():
            continue
        match = ROUND_DIRECTORY.match(directory.name)
        if match is None:
            continue
        input_path = directory / "phase2_input_bench.txt"
        if input_path.is_file():
            found.append((int(match.group(1)), int(match.group(2)), directory))
    return sorted(found)


def load_counts_by_round(log_path):
    """Associate InterChiplet's `Load N delay records` with its Phase-1 round."""
    current_round = None
    counts = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        start = ROUND_START.search(line)
        if start is not None:
            current_round = int(start.group(1))
            continue
        loaded = LOADED_DELAY.search(line)
        if loaded is not None and current_round is not None:
            counts[current_round] = int(loaded.group(1))
    return counts


def validate_round(round_number, directory):
    """Check that one Phase-2 process preserved one complete ns-3 contract."""
    input_path = directory / "phase2_input_bench.txt"
    delay_path = directory / "phase2_delayInfo.txt"
    metrics_path = directory / "phase2_metrics.csv"
    summary_path = directory / "phase2_summary.json"
    required = (delay_path, metrics_path, summary_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("round {} is missing Phase-2 artifact(s): {}".format(round_number, ", ".join(missing)))

    bench_records = line_count(input_path)
    delay_records = line_count(delay_path)
    if bench_records != delay_records:
        raise RuntimeError(
            "round {} bench/delay record mismatch: {} != {}".format(
                round_number, bench_records, delay_records
            )
        )
    with metrics_path.open(newline="", encoding="utf-8") as handle:
        metrics_records = sum(1 for _ in csv.DictReader(handle))
    if bench_records != metrics_records:
        raise RuntimeError(
            "round {} bench/metrics record mismatch: {} != {}".format(
                round_number, bench_records, metrics_records
            )
        )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("phase2_backend") != "ns-3":
        raise RuntimeError("round {} did not report the ns-3 Phase-2 backend".format(round_number))
    return {
        "bench_records": bench_records,
        "delay_records": delay_records,
        "metrics_records": metrics_records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path,
                        help="coordinator timing-artifacts/coordinator directory")
    parser.add_argument("--coordinator-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()

    artifacts = phase_two_directories(arguments.artifact_root)
    if not artifacts:
        raise RuntimeError("no preserved ns-3 Phase-2 process directory exists")
    loaded_counts = load_counts_by_round(arguments.coordinator_log)
    result = {}
    consumed = []
    for round_number, thread_number, directory in artifacts:
        values = validate_round(round_number, directory)
        result["phase2_round_{}_thread_{}_bench_records".format(round_number, thread_number)] = values["bench_records"]
        result["phase2_round_{}_thread_{}_delay_records".format(round_number, thread_number)] = values["delay_records"]
        result["phase2_round_{}_thread_{}_metrics_records".format(round_number, thread_number)] = values["metrics_records"]
        next_round_loaded = loaded_counts.get(round_number + 1, 0)
        result["phase2_round_{}_next_phase1_loaded_records".format(round_number)] = next_round_loaded
        if values["bench_records"] > 0 and next_round_loaded > 0:
            consumed.append(round_number)

    latest_round, latest_thread, _ = artifacts[-1]
    result["latest_phase2_round"] = latest_round
    result["latest_phase2_thread"] = latest_thread
    if consumed:
        result["consumed_phase2_rounds"] = ",".join(str(round_number) for round_number in consumed)
        result["timing_feedback"] = "present"
    else:
        result["timing_feedback"] = "missing"
        raise RuntimeError("ns-3 delayInfo was not loaded as a non-zero next Phase-1 delay list")

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("w", encoding="utf-8") as handle:
        for key in sorted(result):
            handle.write("{}={}\n".format(key, result[key]))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print("timing-feedback validation: {}".format(error), file=sys.stderr)
        sys.exit(1)
