#!/usr/bin/env bash
# Run the documented 1/2/4/8-node MLP-DP DinD scale-out matrix.
#
# The 1/2/4 points use independent Swarms and run concurrently. Once all three
# have naturally completed, their outer DinD containers are stopped before the
# 8-node point is provisioned. This keeps the peak experiment allocation at
# 32 vCPU instead of competing with previous completed points.
set -euo pipefail

image=""
output_root=""
iterations=40
global_samples=32768
delay="1ms"
per_node_cpus=4
per_node_memory_gib=16
image_load_jobs=4
timeout_seconds=2400

usage() {
  cat <<'EOF'
Usage:
  run_dind_mlp_dp_scaleout.sh --image IMAGE --output-root DIRECTORY [options]

Options:
  --iterations N            Fixed training iterations at every point (default: 40).
  --global-samples N        Fixed global batch size (default: 32768).
  --delay DURATION          One-way netem delay for multi-node traffic (default: 1ms).
  --per-node-cpus N         Outer DinD CPU quota for every logical node (default: 4).
  --per-node-memory-gib N   Outer DinD memory quota for every logical node (default: 16).
  --image-load-jobs N       Concurrent nested image imports (default: 4).
  --timeout-seconds N       Coordinator natural-completion timeout (default: 2400).
EOF
}

while (($#)); do
  case "$1" in
    --image) image="$2"; shift 2 ;;
    --output-root) output_root="$2"; shift 2 ;;
    --iterations) iterations="$2"; shift 2 ;;
    --global-samples) global_samples="$2"; shift 2 ;;
    --delay) delay="$2"; shift 2 ;;
    --per-node-cpus) per_node_cpus="$2"; shift 2 ;;
    --per-node-memory-gib) per_node_memory_gib="$2"; shift 2 ;;
    --image-load-jobs) image_load_jobs="$2"; shift 2 ;;
    --timeout-seconds) timeout_seconds="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$image" && -n "$output_root" ]] || { usage >&2; exit 2; }
for value in "$iterations" "$global_samples" "$per_node_cpus" "$per_node_memory_gib" \
             "$image_load_jobs" "$timeout_seconds"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "numeric options must be positive integers" >&2; exit 2; }
done

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_root=$(realpath -m "$output_root")
[[ ! -e "$output_root" ]] || { echo "output root already exists: $output_root" >&2; exit 2; }
mkdir -p "$output_root"

run_tag=$(date +%Y%m%d-%H%M%S)
config_root="$output_root/config"
python3 "$root/scripts/generate_mlp_dp_matrix.py" \
  --output-root "$config_root" --image "$image" --nodes 1 2 4 8 \
  --iterations "$iterations" --global-samples "$global_samples" --ranks-per-node 1

cat >"$output_root/run_metadata.env" <<EOF
IMAGE=$image
ITERATIONS=$iterations
GLOBAL_SAMPLES=$global_samples
NETEM_ONE_WAY_DELAY=$delay
PER_NODE_CPUS=$per_node_cpus
PER_NODE_MEMORY_GIB=$per_node_memory_gib
EOF

prefix_for() { printf 'chipsystemsim-scale-%s-n%s' "$run_tag" "$1"; }

provision_point() {
  local nodes="$1"
  local prefix
  prefix=$(prefix_for "$nodes")
  "$root/scripts/provision_dind_swarm.sh" \
    --nodes "$nodes" --image "$image" \
    --total-cpus "$((nodes * per_node_cpus))" \
    --total-memory "$((nodes * per_node_memory_gib))g" \
    --delay "$delay" --rate none --image-load-jobs "$image_load_jobs" --prefix "$prefix"
}

run_point() {
  local nodes="$1"
  local prefix
  prefix=$(prefix_for "$nodes")
  "$root/scripts/run_dind_mlp_dp.sh" \
    --nodes "$nodes" --prefix "$prefix" \
    --config-dir "$config_root/mlp-dp-nodes${nodes}" \
    --stack "mlp_dp_scale_${run_tag}_n${nodes}" \
    --output-dir "$output_root/nodes${nodes}" \
    --timeout-seconds "$timeout_seconds"
}

stop_point() {
  local nodes="$1"
  local prefix
  prefix=$(prefix_for "$nodes")
  docker ps -q --filter "name=^/${prefix}-" | xargs -r docker stop -t 30 >/dev/null
}

# Provision serially: each provisioning call chooses a free outer bridge subnet.
# Start the actual benchmark processes concurrently only after all three Swarms
# are independently ready.
for nodes in 1 2 4; do provision_point "$nodes"; done

pids=()
for nodes in 1 2 4; do
  run_point "$nodes" >"$output_root/run-nodes${nodes}.log" 2>&1 &
  pids+=("$!")
done
failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done
((failed == 0)) || { echo "at least one of 1/2/4-node runs failed" >&2; exit 1; }

for nodes in 1 2 4; do stop_point "$nodes"; done

provision_point 8
run_point 8 | tee "$output_root/run-nodes8.log"
stop_point 8

python3 "$root/scripts/summarize_dind_mlp_dp.py" \
  --input-root "$output_root" --output "$output_root/summary.csv" --nodes 1 2 4 8
printf 'completed scale-out matrix: %s\n' "$output_root"
