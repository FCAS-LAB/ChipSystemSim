#!/usr/bin/env python3
"""Summarize JSON records written by ``tcp_latency_probe.py``."""

import argparse
import json
import statistics
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    samples = []
    for line in arguments.input.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = record.get("round_trip_ns")
        if isinstance(value, int):
            samples.append(value)
    if not samples:
        raise ValueError("no TCP round-trip records")
    samples.sort()
    summary = {
        "samples": len(samples),
        "definition": "raw TCP echo round trip between the two transport services",
        "round_trip": {
            "min_ms": samples[0] / 1_000_000,
            "median_ms": statistics.median(samples) / 1_000_000,
            "mean_ms": (sum(samples) / float(len(samples))) / 1_000_000,
            "p95_ms": samples[max(0, (95 * len(samples) + 99) // 100 - 1)] / 1_000_000,
            "max_ms": samples[-1] / 1_000_000,
        },
    }
    arguments.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
