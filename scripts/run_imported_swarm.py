#!/usr/bin/env python3
"""Run complete external LEGOSim benchmarks through the Swarm integration.

The runner performs a one-node integration screen: it generates a native
placement, SimBricks topology/routing and a remotely proxied YAML, deploys the
stack, captures all service logs, and then always removes the stack.  Its JSON
summary distinguishes complete native runs from native application failures or
timeouts; none are silently converted into benchmark data.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

import run_real_swarm as core


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
SOURCE_ROOT = WORKSPACE / "single_stage_simulator" / "benchmark"
FAILED_PROCESS = re.compile(r"terminate with status = ([1-9][0-9]*)")
PIN_CRASH = re.compile(r"(?:Pin app terminated abnormally|Tool \(or Pin\) caused signal)", re.IGNORECASE)


def command(arguments: list[str]) -> None:
    core.run(arguments)


def mesh_options(source_yaml: Path) -> tuple[int, int]:
    """Read Popnet's mesh side and flit size from the original YAML."""
    config = yaml.safe_load(source_yaml.read_text(encoding="utf-8"))
    for process in config.get("phase2", []):
        values = list(process.get("args", []))
        try:
            return int(values[values.index("-A") + 1]), int(values[values.index("-F") + 1])
        except (ValueError, IndexError):
            continue
    # The worker bridge itself does not invent an application topology.  These
    # defaults are only the bridge buffer parameters when a YAML has no Popnet.
    return 2, 4


def capture_logs(stack: str, run_dir: Path) -> None:
    for service in ("coordinator", "transport-0"):
        path = run_dir / f"{service}.log"
        try:
            path.write_text(
                core.run(["docker", "service", "logs", f"{stack}_{service}"], capture=True).stdout,
                encoding="utf-8",
            )
        except subprocess.CalledProcessError:
            path.write_text("service logs unavailable\n", encoding="utf-8")


def capture_process_logs(stack: str, run_dir: Path) -> None:
    """Copy InterChiplet per-process output before Swarm removes its task."""
    try:
        container_ids = core.run([
            "docker", "ps", "--filter", f"label=com.docker.swarm.service.name={stack}_coordinator",
            "--format", "{{.ID}}",
        ], capture=True).stdout.strip().splitlines()
        if not container_ids:
            return
        container = container_ids[0]
        paths = core.run([
            "docker", "exec", container, "/bin/bash", "-lc",
            "find /opt/legosim -maxdepth 1 -type d -name 'proc_r*' -print",
        ], capture=True).stdout.splitlines()
        destination = run_dir / "coordinator-process-logs"
        destination.mkdir(exist_ok=True)
        for path in paths:
            if path.strip():
                subprocess.run(["docker", "cp", f"{container}:{path.strip()}", str(destination)],
                               cwd=ROOT, check=False)
    except (RuntimeError, subprocess.CalledProcessError):
        # Log capture must never hide the original benchmark status.
        return


def run_one(benchmark: str, image: str, output_root: Path, timeout_s: int,
            gdb_process_index: int | None = None, transport_ptrace: bool = False,
            sniper_cores: int | None = None, sniper_fast_forward: bool = False,
            sniper_maxthreads: int | None = None) -> dict[str, object]:
    source_dir = SOURCE_ROOT / benchmark
    yaml_paths = sorted(source_dir.glob("*.yml"))
    if not (source_dir / "makefile").is_file() or len(yaml_paths) != 1:
        return {"benchmark": benchmark, "status": "incomplete-source"}
    source_yaml = yaml_paths[0]
    run_dir = output_root / benchmark
    run_dir.mkdir(parents=True, exist_ok=False)
    placement = run_dir / "placement.json"
    topology = run_dir / "topology.json"
    routing = run_dir / "routing.json"
    workload = run_dir / "workload.yml"
    stack_file = run_dir / "stack.yml"
    stack = f"legosim_screen_{benchmark.lower()}"
    width, flit_size = mesh_options(source_yaml)
    result: dict[str, object] = {
        "benchmark": benchmark, "source_yaml": str(source_yaml), "image": image,
        "topology_width": width, "flit_size": flit_size, "status": "pending",
    }
    command([sys.executable, str(ROOT / "real" / "generate_placement.py"), "--source-yaml", str(source_yaml),
             "--workload-name", benchmark, "--nodes", "1", "--output", str(placement)])
    command([sys.executable, str(ROOT / "real" / "generate_simbricks_topology.py"), "--nodes", "1", "--output", str(topology)])
    command([sys.executable, str(ROOT / "real" / "generate_simbricks_routing.py"), "--placement", str(placement),
             "--topology", str(topology), "--output", str(routing)])
    command([sys.executable, str(ROOT / "real" / "generate_distributed_yaml.py"), "--source-yaml", str(source_yaml),
             "--placement", str(placement), "--benchmark-root", f"/opt/legosim/benchmark/{benchmark}",
             *(["--shared-worker-asset", f"/opt/legosim/benchmark/{benchmark}/mapDevice.csv"]
               if (source_dir / "mapDevice.csv").is_file() else []),
             *(["--gdb-process-index", str(gdb_process_index)] if gdb_process_index is not None else []),
             *(["--sniper-cores", str(sniper_cores)] if sniper_cores is not None else []),
             *(["--sniper-fast-forward"] if sniper_fast_forward else []),
             *(["--sniper-maxthreads", str(sniper_maxthreads)] if sniper_maxthreads is not None else []),
             "--stream-output", "--output", str(workload)])
    core.wait_for_stack_removal(stack)
    command([sys.executable, str(ROOT / "real" / "generate_swarm_stack.py"), "--placement", str(placement),
             "--topology", str(topology), "--routing", str(routing), "--workload-yaml", str(workload),
             "--image", image, "--output", str(stack_file), "--stack-name", stack,
             *(["--transport-ptrace"] if transport_ptrace else []),
             "--topology-width", str(width), "--flit-size", str(flit_size)])
    try:
        command(["docker", "stack", "deploy", "--compose-file", str(stack_file), stack])
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = core.coordinator_state(stack)
            # Several upstream coordinators keep another process blocked after
            # a GPU child has crashed.  Detect that definitive native failure
            # from the live coordinator log rather than consuming the complete
            # timeout and preventing the remaining benchmarks from being tried.
            live_log = core.run(["docker", "service", "logs", f"{stack}_coordinator"], capture=True).stdout
            if FAILED_PROCESS.search(live_log) or PIN_CRASH.search(live_log):
                # Give Docker one log-flush interval before the finally block
                # removes services. This retains worker-side ldd diagnostics.
                time.sleep(2)
                result["status"] = "native-process-failed"
                result["native_failures"] = True
                result["pin_crash"] = bool(PIN_CRASH.search(live_log))
                return result
            if state == "Complete":
                capture_logs(stack, run_dir)
                log = (run_dir / "coordinator.log").read_text(encoding="utf-8", errors="replace")
                native_failures = bool(FAILED_PROCESS.search(log) or PIN_CRASH.search(log))
                result["native_failures"] = native_failures
                result["pin_crash"] = bool(PIN_CRASH.search(log))
                if native_failures:
                    result["status"] = "native-process-failed"
                elif "All process has exit" in log:
                    result["status"] = "completed"
                else:
                    result["status"] = "coordinator-completed-without-marker"
                return result
            if state in {"Failed", "Rejected", "missing"}:
                result["status"] = f"coordinator-{state.lower()}"
                return result
            time.sleep(2)
        result["status"] = "timeout"
        return result
    except (RuntimeError, subprocess.CalledProcessError) as error:
        result["status"] = "orchestration-error"
        result["error"] = str(error)
        return result
    finally:
        capture_logs(stack, run_dir)
        capture_process_logs(stack, run_dir)
        subprocess.run(["docker", "stack", "rm", stack], cwd=ROOT, check=False)
        core.wait_for_stack_removal(stack)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmarks", nargs="+", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results" / "single-stage-runs")
    parser.add_argument("--image", help="override the per-benchmark image tag for a diagnostic run")
    parser.add_argument("--gdb-process-index", type=int,
                        help="run this phase-one process under batch gdb (requires a gdb-enabled image)")
    parser.add_argument("--transport-ptrace", action="store_true",
                        help="permit a diagnostic gdb attach inside transport workers")
    parser.add_argument("--sniper-cores", type=int,
                        help="run each Sniper process with this many modeled cores")
    parser.add_argument("--sniper-fast-forward", action="store_true",
                        help="functional CPU fast-forward for end-to-end smoke tests; do not use its timings as benchmark results")
    parser.add_argument("--sniper-maxthreads", type=int,
                        help="Pin recorder host-thread capacity for the Sniper workload")
    arguments = parser.parse_args()
    core.require_active_swarm()
    core.require_node_labels(1)
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for benchmark in arguments.benchmarks:
        image = arguments.image or f"legosim-real:single-stage-{benchmark.lower()}"
        entry = run_one(benchmark, image, arguments.output_dir, arguments.timeout_seconds,
                        arguments.gdb_process_index, arguments.transport_ptrace, arguments.sniper_cores,
                        arguments.sniper_fast_forward, arguments.sniper_maxthreads)
        summary.append(entry)
        (arguments.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
