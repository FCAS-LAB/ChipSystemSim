#!/usr/bin/env python3
"""Run ns-3 Phase 2 and retain the exact per-round protocol inputs/outputs.

InterChiplet uses a fresh ``proc_rN_p2_tM`` directory for every Phase-2
process.  The upstream files ``../bench.txt`` and ``../delayInfo.txt`` are
shared names, so a later convergence round may overwrite ``bench.txt`` after
the previous round's delay file was consumed.  This wrapper copies the exact
input and output beside its process log, making each feedback round auditable.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


def option_value(arguments: List[str], option: str) -> Path:
    try:
        return Path(arguments[arguments.index(option) + 1])
    except (ValueError, IndexError) as error:
        raise ValueError(f"missing {option} argument") from error


def copy_if_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"expected Phase-2 artifact is missing: {source}")
    shutil.copy2(str(source), str(destination))


def main() -> None:
    arguments = sys.argv[1:]
    bench = option_value(arguments, "--bench")
    delay_info = option_value(arguments, "--delay-info")
    metrics = option_value(arguments, "--metrics-csv")
    summary = option_value(arguments, "--summary-json")

    # Snapshot the trace before execution; ns-3 is expected to overwrite only
    # delayInfo and its explicit metrics outputs.
    copy_if_file(bench, Path("phase2_input_bench.txt"))
    executable = os.environ.get("LEGOSIM_NS3_PHASE2_EXECUTABLE", "/usr/local/bin/ns3-phase2")
    subprocess.run([executable, *arguments], check=True)
    copy_if_file(delay_info, Path("phase2_delayInfo.txt"))
    copy_if_file(metrics, Path("phase2_metrics.csv"))
    copy_if_file(summary, Path("phase2_summary.json"))


if __name__ == "__main__":
    main()
