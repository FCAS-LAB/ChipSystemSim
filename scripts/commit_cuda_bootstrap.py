#!/usr/bin/env python3
"""Commit the completed local CUDA bootstrap container into a reusable image.

Docker Desktop in this environment can reach Ubuntu package mirrors but cannot
reliably complete a giant CUDA installation inside a BuildKit layer.  The
bootstrap container retains apt's download progress.  This helper refuses to
commit a running or failed container, creates a tagged image only after the
installation exits successfully, and verifies that ``nvcc`` is present.
"""
from __future__ import annotations

import argparse
import json
import subprocess


def run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=capture)
    return result.stdout if capture else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="legosim-cuda-bootstrap")
    parser.add_argument("--image", default="legosim-cuda:11.5-jammy")
    arguments = parser.parse_args()

    inspect = json.loads(run("docker", "inspect", arguments.container, capture=True))[0]
    state = inspect["State"]
    if state["Running"]:
        raise RuntimeError(f"container {arguments.container} is still running")
    if state["ExitCode"] != 0:
        raise RuntimeError(
            f"container {arguments.container} exited with {state['ExitCode']}; refusing to commit it"
        )

    run("docker", "commit", arguments.container, arguments.image)
    run("docker", "run", "--rm", "--entrypoint", "nvcc", arguments.image, "--version")
    print(f"created and verified {arguments.image}")


if __name__ == "__main__":
    main()
