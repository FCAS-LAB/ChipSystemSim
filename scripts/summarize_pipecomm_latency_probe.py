#!/usr/bin/env python3
"""Summarize paired PipeComm reader/writer latency-probe JSON records."""

import argparse
import json
import statistics
from pathlib import Path


def records(path, expected_event):
    """Read one JSON object per line, retaining only completed probe events."""
    result = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != expected_event:
            continue
        sequence = record.get("sequence")
        if not isinstance(sequence, int):
            raise ValueError(f"{path}: completed record has no integer sequence")
        result[sequence] = record
    return result


def percentiles(values):
    """Return milliseconds; p95 uses the inclusive method for small samples."""
    samples = list(values)
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    return {
        "min_ms": ordered[0] / 1_000_000,
        "median_ms": statistics.median(ordered) / 1_000_000,
        "mean_ms": (sum(ordered) / float(len(ordered))) / 1_000_000,
        "p95_ms": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)] / 1_000_000,
        "max_ms": ordered[-1] / 1_000_000,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader", required=True, type=Path)
    parser.add_argument("--writer", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    reader = records(arguments.reader, "reader_completed")
    writer = records(arguments.writer, "writer_completed")
    common = sorted(set(reader) & set(writer))
    if not common:
        raise ValueError("no completed reader/writer sequence pairs")
    # Docker containers share the Linux monotonic clock.  This is verified by
    # running both commands inside the same DinD host, not across physical VMs.
    # Delivery is the observed time from the writer request submission to the
    # waiting receiver obtaining the payload.  It includes the actual BaseIf
    # and net_sockets path but excludes the intentionally pre-posted read wait.
    delivery = [reader[index]["completed_ns"] - writer[index]["request_issued_ns"]
                for index in common]
    if min(delivery) < 0:
        raise ValueError("container monotonic clocks are not comparable in this environment")
    write_service = [writer[index]["router_write_service_ns"] for index in common]
    read_wait = [reader[index]["router_read_wait_ns"] for index in common]
    summary = {
        "samples": len(common),
        "definition": {
            "delivery_ms": "writer request submitted to reader payload available",
            "router_write_service_ms": "writer submission to local router acknowledgement",
            "router_read_wait_ms": "reader request submitted to router response",
        },
        "delivery": percentiles(delivery),
        "router_write_service": percentiles(write_service),
        "router_read_wait": percentiles(read_wait),
    }
    arguments.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
