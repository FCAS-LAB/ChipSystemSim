#!/usr/bin/env python3
"""Deploy native LEGOSim workloads over a labelled Docker Swarm matrix.

The script only orchestrates runs after the image is present on every Swarm
node. It never falls back to the synthetic node.py adapter. A non-active Swarm,
missing labels, failed service, or timeout is a hard failure.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "real"

WORKLOAD_PARAMETERS = {
    "mlp": (6, 4),
    "dlrm": (4, 4),
    "resnet": (6, 200),
    "bfs": (6, 4),
}


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    # Docker service logs are UTF-8 even when this orchestrator runs under a
    # Windows GBK locale. Decode explicitly so progress-bar glyphs cannot
    # abort result collection after a successful native simulation.
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )


def require_active_swarm() -> None:
    result = run(["docker", "info", "--format", "{{.Swarm.LocalNodeState}}"], capture=True)
    if result.stdout.strip() != "active":
        raise RuntimeError("Docker Swarm is not active on this manager")


def require_node_labels(node_count: int) -> None:
    node_lines = run(["docker", "node", "ls", "--format", "{{json .}}"], capture=True).stdout.splitlines()
    nodes = [json.loads(line) for line in node_lines if line]
    if len(nodes) < node_count:
        raise RuntimeError(f"need {node_count} Swarm nodes, found {len(nodes)}")
    # Docker's `node ls --filter label=key=value` does not match label keys
    # containing dots on Docker Desktop. Inspect the declared labels directly
    # so the preflight check agrees with the stack constraints.
    labels_by_node: list[dict[str, str]] = []
    for node in nodes:
        inspected = run(["docker", "node", "inspect", node["ID"]], capture=True)
        labels = json.loads(inspected.stdout)[0].get("Spec", {}).get("Labels", {}) or {}
        labels_by_node.append({str(key): str(value) for key, value in labels.items()})
    for slot in range(node_count):
        expected = f"legosim.node.{slot}"
        if not any(labels.get(expected) == "true" for labels in labels_by_node):
            raise RuntimeError(f"no Swarm node has label legosim.node.{slot}=true")


def coordinator_state(stack: str) -> str:
    """Return the useful aggregate state of the coordinator service.

    Swarm retains short-lived rejected tasks while it creates a replacement.
    Looking only at the first row can therefore report ``Rejected`` even when
    the replacement coordinator is running. Prefer completion and running
    tasks, and report a failure only when no viable task remains.
    """
    result = run(
        ["docker", "service", "ps", f"{stack}_coordinator", "--format", "{{json .}}"],
        capture=True,
    )
    tasks = [json.loads(line) for line in result.stdout.splitlines() if line]
    states = [str(task.get("CurrentState", "")) for task in tasks]
    if any(state.startswith("Complete") for state in states):
        return "Complete"
    if any(state.startswith("Running") for state in states):
        return "Running"
    if any(state.startswith("Failed") for state in states):
        return "Failed"
    if any(state.startswith("Rejected") for state in states):
        return "Rejected"
    return states[0] if states else "missing"


def wait_for_stack_removal(stack: str, timeout_s: int = 60) -> None:
    """Wait for Swarm's asynchronous stack deletion before reusing resources."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        stacks = run(["docker", "stack", "ls", "--format", "{{.Name}}"], capture=True).stdout.splitlines()
        networks = run(["docker", "network", "ls", "--format", "{{.Name}}"], capture=True).stdout.splitlines()
        if stack not in stacks and f"{stack}_legosim" not in networks:
            return
        time.sleep(1)
    raise TimeoutError(f"Swarm did not remove stack {stack} within {timeout_s} seconds")


def capture_process_logs(stack: str, run_dir: Path) -> None:
    """Copy upstream InterChiplet logs before the coordinator task is removed."""
    try:
        containers = run([
            "docker", "ps", "--filter", f"label=com.docker.swarm.service.name={stack}_coordinator",
            "--format", "{{.ID}}",
        ], capture=True).stdout.splitlines()
        if not containers:
            return
        container = containers[0].strip()
        source_paths = run([
            "docker", "exec", container, "/bin/bash", "-lc",
            "find /opt/legosim -maxdepth 1 -type d -name 'proc_r*' -print",
        ], capture=True).stdout.splitlines()
        destination = run_dir / "coordinator-process-logs"
        destination.mkdir(exist_ok=True)
        for source_path in source_paths:
            if source_path.strip():
                subprocess.run(["docker", "cp", f"{container}:{source_path.strip()}", str(destination)],
                               cwd=ROOT, check=False)
    except (RuntimeError, subprocess.CalledProcessError):
        # Capturing evidence must not mask the coordinator's native status.
        return


def run_one(workload: str, node_count: int, image: str, output_root: Path, timeout_s: int,
            sniper_cores: int | None, sniper_maxthreads: int | None, stream_output: bool) -> None:
    width, flit_size = WORKLOAD_PARAMETERS[workload]
    run_dir = output_root / f"{workload}-nodes{node_count}"
    run_dir.mkdir(parents=True, exist_ok=False)
    placement = run_dir / "placement.json"
    topology = run_dir / "topology.json"
    routing = run_dir / "routing.json"
    derived_yaml = run_dir / "workload.yml"
    stack_file = run_dir / "stack.yml"
    coordinator_log = run_dir / "coordinator.log"
    service_logs = {
        "coordinator": coordinator_log,
        **{f"transport-{slot}": run_dir / f"transport-{slot}.log" for slot in range(node_count)},
    }
    manifest = json.loads((REAL / "workloads.json").read_text(encoding="utf-8"))
    source_yaml = ROOT.parent / "LEGOSIM_MICRO" / manifest["workloads"][workload]["yaml"]
    # The coordinator receives a derived YAML in a container. Expand the
    # upstream placeholder to its matching image path instead of relying on a
    # host-only BENCHMARK_ROOT environment variable.
    benchmark_root = "/opt/legosim/" + str(source_yaml.parent.relative_to(ROOT.parent / "LEGOSIM_MICRO")).replace("\\", "/")
    run([sys.executable, str(REAL / "generate_placement.py"), workload, "--nodes", str(node_count), "--output", str(placement)])
    run([sys.executable, str(REAL / "generate_simbricks_topology.py"), "--nodes", str(node_count), "--output", str(topology)])
    run([sys.executable, str(REAL / "generate_simbricks_routing.py"), "--placement", str(placement),
         "--topology", str(topology), "--output", str(routing)])
    run([sys.executable, str(REAL / "generate_distributed_yaml.py"), "--source-yaml", str(source_yaml),
         "--placement", str(placement), "--benchmark-root", benchmark_root,
         *(["--sniper-cores", str(sniper_cores)] if sniper_cores is not None else []),
         *(["--sniper-maxthreads", str(sniper_maxthreads)] if sniper_maxthreads is not None else []),
         "--output", str(derived_yaml),
         *(["--stream-output"] if stream_output else [])])
    stack = f"legosim_{workload}_{node_count}"
    # A prior interrupted run can leave Swarm asynchronously deleting the
    # overlay network. Do not reuse the stack name until both resources are gone.
    wait_for_stack_removal(stack)
    run([sys.executable, str(REAL / "generate_swarm_stack.py"), "--placement", str(placement),
         "--topology", str(topology), "--routing", str(routing),
         "--workload-yaml", str(derived_yaml), "--image", image, "--output", str(stack_file),
         "--stack-name", stack, "--topology-width", str(width), "--flit-size", str(flit_size)])
    try:
        run(["docker", "stack", "deploy", "--compose-file", str(stack_file), stack])
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = coordinator_state(stack)
            if state.startswith("Complete"):
                log_text = run(["docker", "service", "logs", f"{stack}_coordinator"], capture=True).stdout
                coordinator_log.write_text(log_text, encoding="utf-8")
                child_failures = re.findall(r"terminate with status = ([1-9][0-9]*)", log_text)
                if child_failures:
                    raise RuntimeError(
                        "coordinator exited after failed native phase process(es): "
                        + ", ".join(child_failures)
                    )
                return
            if state.startswith("Failed") or state.startswith("Rejected"):
                raise RuntimeError(f"coordinator failed: {state}")
            time.sleep(2)
        raise TimeoutError(f"coordinator did not complete within {timeout_s} seconds")
    finally:
        # Capture every service before stack removal. This preserves evidence
        # for completion and makes remote process-proxy failures diagnosable.
        for service, log_path in service_logs.items():
            if log_path.exists():
                continue
            try:
                log_path.write_text(
                    run(["docker", "service", "logs", f"{stack}_{service}"], capture=True).stdout,
                    encoding="utf-8",
                )
            except subprocess.CalledProcessError:
                pass
        capture_process_logs(stack, run_dir)
        removal = subprocess.run(["docker", "stack", "rm", stack], cwd=ROOT, text=True)
        if removal.returncode != 0:
            print(f"warning: stack removal returned {removal.returncode}")
        wait_for_stack_removal(stack)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--image",
        help=(
            "default image for every selected workload; retained for a single-image build"
        ),
    )
    parser.add_argument(
        "--workload-image",
        action="append",
        default=[],
        metavar="WORKLOAD=IMAGE",
        help=(
            "override the image for one workload; repeat as needed, for example "
            "--workload-image dlrm=registry/legosim-real:native-dlrm"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results" / "real-swarm")
    parser.add_argument("--workloads", nargs="+", choices=sorted(WORKLOAD_PARAMETERS), default=sorted(WORKLOAD_PARAMETERS))
    parser.add_argument("--nodes", nargs="+", type=int, choices=(1, 2, 4, 8), default=(1, 2, 4, 8))
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--sniper-cores", type=int,
                        help="modeled cores for upstream Sniper phase-one processes")
    parser.add_argument("--sniper-maxthreads", type=int,
                        help="Pin recorder capacity for upstream Sniper application threads")
    parser.add_argument("--stream-output", action="store_true",
                        help="forward all native child output to coordinator service logs (diagnostic only)")
    arguments = parser.parse_args()
    workload_images: dict[str, str] = {}
    for item in arguments.workload_image:
        workload, separator, image = item.partition("=")
        if separator != "=" or workload not in WORKLOAD_PARAMETERS or not image:
            parser.error(
                "--workload-image must be WORKLOAD=IMAGE, where WORKLOAD is one of "
                + ", ".join(sorted(WORKLOAD_PARAMETERS))
            )
        if workload in workload_images:
            parser.error(f"duplicate --workload-image for {workload}")
        workload_images[workload] = image
    missing_images = [
        workload for workload in arguments.workloads
        if workload not in workload_images and not arguments.image
    ]
    if missing_images:
        parser.error(
            "provide --image or --workload-image for: " + ", ".join(missing_images)
        )
    require_active_swarm()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    for node_count in arguments.nodes:
        require_node_labels(node_count)
        for workload in arguments.workloads:
            image = workload_images.get(workload, arguments.image)
            assert image is not None  # Enforced by the missing-image validation above.
            run_one(workload, node_count, image, arguments.output_dir, arguments.timeout_seconds,
                    arguments.sniper_cores, arguments.sniper_maxthreads, arguments.stream_output)


if __name__ == "__main__":
    main()
