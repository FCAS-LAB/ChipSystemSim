#!/usr/bin/env bash
# Run the 1/2/4-node MLP-DP matrix on one reusable four-node DinD Swarm.
#
# All three measurements use the same outer-image archive and the same four
# nested Docker daemons. Before each point, only the participating DinD nodes
# receive the fixed total CPU/memory budget; no simulated service is placed on
# the remaining idle nodes.
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_root=""
image="chipsystemsim:mlp-dp-tree-v5"
image_archive=""
prefix="chipsystemsim-mlp-dp-reuse"
total_cpus=64
total_memory="128g"
delay="1ms"
iterations=3
global_samples=32768
repetitions=1

usage() {
  cat <<'EOF'
Usage: run_dind_mlp_dp_reuse_matrix.sh --output-root DIR --image-archive FILE [options]

Runs 1, 2, and 4 logical-node MLP-DP points sequentially on one reusable
four-node DinD Swarm. The image archive must be a Docker archive for --image.

Options:
  --image IMAGE           Simulation image (default: chipsystemsim:mlp-dp-tree-v5)
  --image-archive FILE    Pre-exported Docker image archive (required)
  --prefix NAME           Dedicated DinD container prefix
  --total-cpus N          Fixed aggregate CPU quota (default: 64)
  --total-memory SIZE     Fixed aggregate memory cap (default: 128g)
  --delay DURATION        One-way netem delay (default: 1ms)
  --iterations N          MLP-DP iterations (default: 3)
  --global-samples N      Fixed global samples (default: 32768)
  --repetitions N         Consecutive repetitions per node count (default: 1)
EOF
}

while (($#)); do
  case "$1" in
    --output-root) output_root="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --image-archive) image_archive="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --total-cpus) total_cpus="$2"; shift 2 ;;
    --total-memory) total_memory="$2"; shift 2 ;;
    --delay) delay="$2"; shift 2 ;;
    --iterations) iterations="$2"; shift 2 ;;
    --global-samples) global_samples="$2"; shift 2 ;;
    --repetitions) repetitions="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$output_root" && -n "$image_archive" ]] || { usage >&2; exit 2; }
[[ "$total_cpus" =~ ^[1-9][0-9]*$ ]] || { echo "--total-cpus must be positive" >&2; exit 2; }
[[ "$repetitions" =~ ^[1-9][0-9]*$ ]] || { echo "--repetitions must be positive" >&2; exit 2; }
[[ -r "$image_archive" ]] || { echo "image archive is not readable: $image_archive" >&2; exit 2; }
[[ ! -e "$output_root" ]] || { echo "refusing to overwrite: $output_root" >&2; exit 2; }

mkdir -p "$output_root/config" "$output_root/dind-storage"

cleanup() {
  "$project_root/scripts/provision_dind_swarm.sh" --nodes 4 --prefix "$prefix" \
    --dind-data-root "$output_root/dind-storage" --cleanup || true
}
trap cleanup EXIT

python3 "$project_root/scripts/generate_mlp_dp_matrix.py" \
  --output-root "$output_root/config" --image "$image" --nodes 1 2 4 \
  --iterations "$iterations" --global-samples "$global_samples" --ranks-per-node 1

"$project_root/scripts/provision_dind_swarm.sh" \
  --nodes 4 --dind-image chipsystemsim:dind-netem --image "$image" \
  --image-archive "$image_archive" --total-cpus "$total_cpus" \
  --total-memory "$total_memory" --delay "$delay" --rate none \
  --image-load-jobs 4 --dind-data-root "$output_root/dind-storage" --prefix "$prefix"

total_memory_bytes=$(python3 - "$total_memory" <<'PY'
import re
import sys

value = sys.argv[1].strip().lower()
match = re.fullmatch(r"(\d+)([kmgt]i?b?|)", value)
if not match:
    raise SystemExit(f"unsupported memory size: {value}")
number, suffix = match.groups()
power = {"": 0, "k": 1, "kb": 1, "kib": 1, "m": 2, "mb": 2, "mib": 2,
         "g": 3, "gb": 3, "gib": 3, "t": 4, "tb": 4, "tib": 4}[suffix]
print(int(number) * (1024 ** power))
PY
)

set_node_limits() {
  local active_nodes="$1"
  local active_cpus=$((total_cpus / active_nodes))
  local active_memory_bytes=$((total_memory_bytes / active_nodes))
  local slot

  # Docker limits are upper bounds, not reservations. Idle DinD daemons remain
  # available for the next point but run no simulation services.
  for slot in 0 1 2 3; do
    if ((slot < active_nodes)); then
      # Docker rejects a raised memory cap when the older memory-swap cap is
      # smaller. Set both limits together; equal values disable swap and keep
      # the fixed-memory resource contract explicit.
      docker update --cpus "$active_cpus" --memory "$active_memory_bytes" \
        --memory-swap "$active_memory_bytes" "${prefix}-${slot}" >/dev/null
    else
      docker update --cpus 1 --memory 4g --memory-swap 4g "${prefix}-${slot}" >/dev/null
    fi
  done
}

for repeat in $(seq 1 "$repetitions"); do
  run_root="$output_root/runs/run${repeat}"
  for nodes in 1 2 4; do
    set_node_limits "$nodes"
    "$project_root/scripts/run_dind_mlp_dp.sh" \
      --nodes "$nodes" --prefix "$prefix" \
      --config-dir "$output_root/config/mlp-dp-nodes${nodes}" \
      --stack "chipsystemsim_mlp_dp_reuse_${nodes}_r${repeat}" \
      --output-dir "$run_root/nodes${nodes}" --timeout-seconds 1600
  done
  python3 "$project_root/scripts/summarize_dind_mlp_dp.py" \
    --input-root "$run_root" --output "$run_root/summary.csv" --nodes 1 2 4
done

python3 - "$output_root" "$repetitions" <<'PY'
import csv
import statistics
import sys
from pathlib import Path

output_root = Path(sys.argv[1])
repetitions = int(sys.argv[2])
rows_by_node: dict[int, list[dict[str, float]]] = {1: [], 2: [], 4: []}
for repeat in range(1, repetitions + 1):
    with (output_root / "runs" / f"run{repeat}" / "summary.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            nodes = int(row["nodes"])
            rows_by_node[nodes].append({key: float(value) for key, value in row.items() if key != "nodes"})

fieldnames = ["nodes", "repetitions", "mean_total_simulation_seconds", "stdev_total_simulation_seconds",
              "mean_cross_node_sync_wall_seconds", "mean_cross_node_sync_overhead_percent",
              "mean_cross_node_bytes", "mean_cross_node_records"]
with (output_root / "summary_average.csv").open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for nodes in (1, 2, 4):
        rows = rows_by_node[nodes]
        totals = [row["total_simulation_seconds"] for row in rows]
        writer.writerow({
            "nodes": nodes,
            "repetitions": len(rows),
            "mean_total_simulation_seconds": statistics.fmean(totals),
            "stdev_total_simulation_seconds": statistics.stdev(totals) if len(totals) > 1 else 0.0,
            "mean_cross_node_sync_wall_seconds": statistics.fmean(row["cross_node_sync_wall_seconds"] for row in rows),
            "mean_cross_node_sync_overhead_percent": statistics.fmean(row["cross_node_sync_overhead_percent"] for row in rows),
            "mean_cross_node_bytes": statistics.fmean(row["cross_node_bytes"] for row in rows),
            "mean_cross_node_records": statistics.fmean(row["cross_node_records"] for row in rows),
        })
PY

echo "MLP_DP_REUSE_MATRIX_SUCCESS=$output_root"
