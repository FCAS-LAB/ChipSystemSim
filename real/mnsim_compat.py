#!/usr/bin/env python3
"""Source-calibrated compatibility backend for LEGOSim's MNSIM task.

The upstream MNSIM task consumes a fixed ``result_0_2.res`` scalar after
receiving its PipeComm payload.  Its private MNSIM fork was not published, but
the scalar used by the checked-in benchmark is available in the source tree.
This adapter keeps the native task and communication path intact, exposes the
calibration explicitly, and records every invocation for later analysis.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


# D:\root\work2026\LEGOSIM_MICRO\MNSIMChiplet\result_0_2.res, shipped by
# the upstream benchmark and read unconditionally by all three mnsim.cpp
# implementations (MLP, ResNet, and BFS).
SOURCE_CALIBRATED_CYCLES = 42_892_008


def main() -> None:
    parser = argparse.ArgumentParser(description="LEGOSim MNSIM compatibility backend")
    parser.add_argument("--workload", required=True, choices=("mlp", "resnet", "bfs"))
    parser.add_argument("--id1", required=True, type=int)
    parser.add_argument("--id2", required=True, type=int)
    parser.add_argument("--payload-elements", required=True, type=int)
    parser.add_argument("--element-bytes", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    arguments = parser.parse_args()

    if arguments.payload_elements < 1 or arguments.element_bytes < 1:
        raise ValueError("payload dimensions must be positive")

    arguments.output.write_text(f"{SOURCE_CALIBRATED_CYCLES}\n", encoding="ascii")
    audit = {
        "backend": "source-calibrated-mnsim-compat-v1",
        "calibration_source": "MNSIMChiplet/result_0_2.res",
        "calibrated_cycles": SOURCE_CALIBRATED_CYCLES,
        "workload": arguments.workload,
        "chiplet": [arguments.id1, arguments.id2],
        "payload_bytes": arguments.payload_elements * arguments.element_bytes,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    arguments.audit.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
