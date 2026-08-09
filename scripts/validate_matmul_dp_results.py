#!/usr/bin/env python3
"""Validate the exact-result marker produced by native block-GEMM."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


GLOBAL_ROWS = 480
INNER = 64
COLUMNS = 64
EXPECTED_CHECKSUM = INNER * sum(range(1, GLOBAL_ROWS + 1)) * sum(range(1, COLUMNS + 1))
MARKER = re.compile(
    r"MATMUL_DP_RESULT verification=ok ranks=(?P<ranks>\d+) "
    r"global_rows=(?P<rows>\d+) inner=(?P<inner>\d+) columns=(?P<columns>\d+) "
    r"checksum=(?P<checksum>\d+) steady_ns=(?P<steady_ns>\d+)"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coordinator-log", type=Path, required=True)
    parser.add_argument("--ranks", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.ranks < 1 or arguments.ranks > 35 or GLOBAL_ROWS % arguments.ranks:
        raise ValueError("--ranks must be a supported divisor of 480")
    matches = list(MARKER.finditer(arguments.coordinator_log.read_text(encoding="utf-8", errors="replace")))
    if not matches:
        raise RuntimeError("missing MATMUL_DP_RESULT marker")
    expected = {
        "ranks": arguments.ranks,
        "rows": GLOBAL_ROWS,
        "inner": INNER,
        "columns": COLUMNS,
        "checksum": EXPECTED_CHECKSUM,
    }
    parsed = [{key: int(value) for key, value in match.groupdict().items()} for match in matches]
    for round_index, result in enumerate(parsed, start=1):
        mismatches = [f"{key}={result[key]} expected={value}"
                      for key, value in expected.items() if result[key] != value]
        if mismatches:
            raise RuntimeError(
                f"invalid block-GEMM marker in simulation round {round_index}: " +
                ", ".join(mismatches)
            )
    # LEGOSim runs the phase-one/phase-two pair repeatedly until the phase-two
    # delay feedback reaches a stable final round.  Every marker must match;
    # retain the final round's host-side dispatch interval as the steady value.
    result = parsed[-1]
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        "verification=ok\n"
        f"completed_simulation_rounds={len(parsed)}\n"
        f"ranks={result['ranks']}\n"
        f"global_rows={result['rows']}\n"
        f"inner={result['inner']}\n"
        f"columns={result['columns']}\n"
        f"checksum={result['checksum']}\n"
        f"steady_ns={result['steady_ns']}\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
