#!/usr/bin/env python3
"""Wrap native LEGOSim executables with remote-worker process proxies."""
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
    parser.add_argument("--remote-phase2", action="store_true",
                        help="run phase-two PopNet on its placement worker and stage its generated inputs")
    parser.add_argument("--phase2-backend", choices=("popnet", "ns3"), default="popnet",
                        help="retain upstream PopNet or replace it with the ns-3 bench/delayInfo adapter")
    parser.add_argument("--ns3-cycle-ns", type=int, default=1,
                        help="nanoseconds represented by one Phase-2 cycle when --phase2-backend=ns3")
    parser.add_argument("--ns3-link-rate", default="128Gbps",
                        help="ns-3 point-to-point link rate when --phase2-backend=ns3")
    parser.add_argument("--ns3-link-delay-ns", type=int, default=1,
                        help="ns-3 propagation delay per topology edge when --phase2-backend=ns3")
    parser.add_argument("--ns3-queue-packets", type=int, default=100000,
                        help="ns-3 per-link DropTail queue bound when --phase2-backend=ns3")
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
    if arguments.ns3_cycle_ns < 1 or arguments.ns3_link_delay_ns < 1 or arguments.ns3_queue_packets < 1:
        raise ValueError("ns-3 timing parameters must be positive")
    if arguments.remote_phase2 and arguments.phase2_backend == "ns3":
        raise ValueError("ns-3 Phase 2 must remain coordinator-local so delayInfo is available to the next round")
    config = yaml.safe_load(arguments.source_yaml.read_text(encoding="utf-8"))
    placement = json.loads(arguments.placement.read_text(encoding="utf-8"))
    placements_by_phase = {
        phase: {item["process_index"]: item for item in placement["processes"] if item["phase"] == phase}
        for phase in ("phase1", "phase2")
    }

    def wrap_process(process: dict[str, object], phase: str, index: int) -> None:
        worker = f"worker-{placements_by_phase[phase][index]['node_slot']}:9300"
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
        if phase == "phase1" and arguments.gdb_process_index == index:
            # Keep the proxy protocol unchanged: gdb simply becomes the remote
            # child and emits its backtrace on the child's stderr stream.
            command, command_arguments = "/usr/bin/gdb", [
                "--batch", "--quiet", "-ex", "set pagination off",
                "-ex", "run", "-ex", "thread apply all bt full", "--args",
                command, *command_arguments,
            ]
        elif phase == "phase1" and arguments.sniper_cores is not None and command.endswith("/run-sniper"):
            if arguments.sniper_cores < 1:
                raise ValueError("--sniper-cores must be positive")
            command_arguments = ["-n", str(arguments.sniper_cores), *command_arguments]
        if phase == "phase1" and arguments.sniper_fast_forward and command.endswith("/run-sniper"):
            command_arguments = ["--fast-forward", *command_arguments]
        if phase == "phase1" and arguments.sniper_maxthreads is not None and command.endswith("/run-sniper"):
            if arguments.sniper_maxthreads < 1:
                raise ValueError("--sniper-maxthreads must be positive")
            command_arguments = [f"--maxthreads={arguments.sniper_maxthreads}", *command_arguments]
        staged_arguments: list[str] = []
        if phase == "phase2":
            # Upstream PopNet addresses phase-one output and static topology
            # inputs as ../name from its local proc_r*_p2_t* directory.  The
            # proxy stages those files from that coordinator directory, then
            # rewrites each path to the remote child-local basename.
            for argument_index, value in enumerate(command_arguments):
                if not isinstance(value, str) or not value.startswith("../"):
                    continue
                # Keep the upstream Linux path spelling even when this
                # generator itself runs on Windows.  pathlib.Path would emit
                # ``..\\bench.txt``, which is a literal filename in a Linux
                # coordinator container rather than a parent-directory path.
                source = value
                destination = source.rsplit("/", 1)[-1]
                command_arguments[argument_index] = destination
                staged_arguments.extend(["--stage-file", f"{source}:{destination}"])
        process["cmd"] = "python3"
        process["args"] = [
            "/opt/chipsystemsim-distributed/process_proxy.py", "--worker", worker,
            "--process-id", f"{phase}-{index}", "--pre-copy", pre_copy,
            *staged_arguments,
            *[argument for asset in shared_assets for argument in ("--shared-asset", asset)],
            *(
                ["--gpgpu-max-completed-cta", str(arguments.gpgpu_max_completed_cta)]
                if phase == "phase1" and arguments.gpgpu_max_completed_cta is not None and "gpgpu-sim" in pre_copy
                else []
            ),
            *(
                ["--gpgpu-max-instructions", str(arguments.gpgpu_max_instructions)]
                if phase == "phase1" and arguments.gpgpu_max_instructions is not None and "gpgpu-sim" in pre_copy
                else []
            ),
            command, *command_arguments,
        ]
        process.pop("pre_copy", None)
        if arguments.stream_output:
            process["is_to_stdout"] = True
    if arguments.phase2_backend == "ns3":
        for process in config.get("phase2", []):
            original_args = list(process.get("args", []))
            try:
                node_count = str(original_args[original_args.index("-A") + 1])
                flit_count = int(original_args[original_args.index("-F") + 1])
                topology = str(original_args[original_args.index("-G") + 1])
            except (ValueError, IndexError) as error:
                raise ValueError("Phase-2 PopNet arguments must contain -A, -F and -G") from error
            if flit_count < 1:
                raise ValueError("Phase-2 flit count must be positive")
            # Phase 2 remains in the coordinator container. Upstream PopNet
            # resolves ``../topology/...`` from its per-process directory only
            # when launched from the benchmark tree; the distributed
            # coordinator instead starts in /opt/legosim. Resolve that input
            # against the explicit in-image benchmark root so ns-3 sees the
            # same topology file without relying on an undeclared working
            # directory layout.
            if topology.startswith("../topology/") and arguments.benchmark_root:
                topology = str(
                    Path(arguments.benchmark_root) / "topology" / Path(topology).name
                )
            process["cmd"] = "python3"
            process["args"] = [
                "/opt/chipsystemsim-distributed/ns3_phase2_runner.py",
                "--bench", "../bench.txt", "--delay-info", "../delayInfo.txt",
                "--topology", topology, "--nodes", node_count,
                "--cycle-ns", str(arguments.ns3_cycle_ns),
                "--flit-bytes", str(flit_count * 8),
                "--link-rate", arguments.ns3_link_rate,
                "--link-delay-ns", str(arguments.ns3_link_delay_ns),
                "--queue-packets", str(arguments.ns3_queue_packets),
                "--metrics-csv", "../ns3_phase2_metrics.csv",
                "--summary-json", "../ns3_phase2_summary.json",
            ]
            process["log"] = "ns3_phase2.log"
            process["is_to_stdout"] = True

    for index, process in enumerate(config["phase1"]):
        wrap_process(process, "phase1", index)
    if arguments.remote_phase2:
        for index, process in enumerate(config.get("phase2", [])):
            wrap_process(process, "phase2", index)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
