#!/usr/bin/env python3
"""Run a local-FIFO versus remote-PipeComm comparison for one benchmark.

This deliberately reuses the exact benchmark image and YAML used by Swarm. It
does not claim to fix memory corruption: its output establishes whether the
fault exists in the base GPGPU-Sim process before the remote compatibility
layer is involved.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--yaml", required=True, help="absolute YAML path inside the image")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--flit-size", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "docker", "run", "--rm", "--entrypoint", "/usr/local/bin/legosim-run", arguments.image,
        arguments.yaml, "-w", str(arguments.width), "-f", str(arguments.flit_size),
    ]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True)
    arguments.output.write_text(
        f"command: {' '.join(command)}\nexit_code: {completed.returncode}\n\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}",
        encoding="utf-8",
    )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
