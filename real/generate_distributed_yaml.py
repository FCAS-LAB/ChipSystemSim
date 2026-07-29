#!/usr/bin/env python3
"""Wrap phase-1 LEGOSim executables with remote-worker process proxies."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-yaml", type=Path, required=True)
    parser.add_argument("--placement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stream-output", action="store_true",
                        help="emit wrapped phase-one stdout/stderr to coordinator logs for diagnosis")
    parser.add_argument("--benchmark-root",
                        help="replace $BENCHMARK_ROOT for YAMLs copied into a container image")
    parser.add_argument("--shared-worker-asset", action="append", default=[],
                        help="file or glob copied into the worker run root before every phase-one spawn")
    parser.add_argument("--gdb-process-index", type=int,
                        help="run exactly one phase-one process under batch gdb and print all thread backtraces")
    parser.add_argument("--sniper-cores", type=int,
                        help="prepend -n CORES to each Sniper phase-one command")
    parser.add_argument("--sniper-fast-forward", action="store_true",
                        help="run Sniper CPU instructions in functional fast-forward mode; use only for an end-to-end protocol smoke test")
    parser.add_argument("--sniper-maxthreads", type=int,
                        help="pass --maxthreads to Sniper's Pin recorder; set this to the application's total host-thread count")
    parser.add_argument("--gpgpu-max-completed-cta", type=int,
                        help="cap completed CTAs in each GPGPU-Sim phase-one process for a functional smoke test")
    parser.add_argument("--gpgpu-max-instructions", type=int,
                        help="cap simulated GPU instructions in each GPGPU-Sim phase-one process for a functional smoke test")
    arguments = parser.parse_args()
    if arguments.gpgpu_max_completed_cta is not None and arguments.gpgpu_max_completed_cta < 1:
        raise ValueError("--gpgpu-max-completed-cta must be positive")
    if arguments.gpgpu_max_instructions is not None and arguments.gpgpu_max_instructions < 1:
        raise ValueError("--gpgpu-max-instructions must be positive")
    config = yaml.safe_load(arguments.source_yaml.read_text(encoding="utf-8"))
    placement = json.loads(arguments.placement.read_text(encoding="utf-8"))
    phase1 = {item["process_index"]: item for item in placement["processes"] if item["phase"] == "phase1"}
    for index, process in enumerate(config["phase1"]):
        worker = f"worker-{phase1[index]['node_slot']}:9300"
        command = process["cmd"]
        command_arguments = list(process.get("args", []))
        pre_copy = process.get("pre_copy", "")
        if arguments.benchmark_root:
            command = command.replace("$BENCHMARK_ROOT", arguments.benchmark_root)
            command_arguments = [
                value.replace("$BENCHMARK_ROOT", arguments.benchmark_root)
                if isinstance(value, str) else value
                for value in command_arguments
            ]
            pre_copy = pre_copy.replace("$BENCHMARK_ROOT", arguments.benchmark_root)
        shared_assets = [
            asset.replace("$BENCHMARK_ROOT", arguments.benchmark_root)
            if arguments.benchmark_root else asset
            for asset in arguments.shared_worker_asset
        ]
        if arguments.gdb_process_index == index:
            # Keep the proxy protocol unchanged: gdb simply becomes the remote
            # child and emits its backtrace on the child's stderr stream.
            command, command_arguments = "/usr/bin/gdb", [
                "--batch", "--quiet", "-ex", "set pagination off",
                "-ex", "run", "-ex", "thread apply all bt full", "--args",
                command, *command_arguments,
            ]
        elif arguments.sniper_cores is not None and command.endswith("/run-sniper"):
            if arguments.sniper_cores < 1:
                raise ValueError("--sniper-cores must be positive")
            command_arguments = ["-n", str(arguments.sniper_cores), *command_arguments]
        if arguments.sniper_fast_forward and command.endswith("/run-sniper"):
            command_arguments = ["--fast-forward", *command_arguments]
        if arguments.sniper_maxthreads is not None and command.endswith("/run-sniper"):
            if arguments.sniper_maxthreads < 1:
                raise ValueError("--sniper-maxthreads must be positive")
            command_arguments = [f"--maxthreads={arguments.sniper_maxthreads}", *command_arguments]
        process["cmd"] = "python3"
        process["args"] = [
            "/opt/legosim-distributed/process_proxy.py", "--worker", worker,
            "--process-id", f"phase1-{index}", "--pre-copy", pre_copy,
            *[argument for asset in shared_assets for argument in ("--shared-asset", asset)],
            *(
                ["--gpgpu-max-completed-cta", str(arguments.gpgpu_max_completed_cta)]
                if arguments.gpgpu_max_completed_cta is not None and "gpgpu-sim" in pre_copy
                else []
            ),
            *(
                ["--gpgpu-max-instructions", str(arguments.gpgpu_max_instructions)]
                if arguments.gpgpu_max_instructions is not None and "gpgpu-sim" in pre_copy
                else []
            ),
            command, *command_arguments,
        ]
        process.pop("pre_copy", None)
        if arguments.stream_output:
            process["is_to_stdout"] = True
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
