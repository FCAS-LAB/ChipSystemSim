#!/usr/bin/env python3
"""Verify that the native-workload manifest matches an upstream checkout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legosim-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("workloads.json"))
    arguments = parser.parse_args()
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    for name, workload in manifest["workloads"].items():
        source = arguments.legosim_root / workload["yaml"]
        if not source.is_file():
            raise FileNotFoundError(f"{name}: missing {source}")
        config = yaml.safe_load(source.read_text(encoding="utf-8"))
        for phase in ("phase1", "phase2"):
            expected = workload[f"{phase}_processes"]
            actual = len(config.get(phase, []))
            if actual != expected:
                raise ValueError(f"{name}: {phase} count is {actual}, expected {expected}")
    print("native workload manifest: passed")


if __name__ == "__main__":
    main()
