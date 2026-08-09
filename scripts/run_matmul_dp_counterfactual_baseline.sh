#!/usr/bin/env bash
# Collect a cycle-domain counterfactual for cross-LEGOSim communication.
#
# The actual and baseline runs retain the same 32-rank block-GEMM workload,
# placement and PipeComm data path.  The baseline changes only ns-3 delayInfo:
# traffic crossing logical LEGOSim workers receives zero simulated network
# delay.  Its final LEGOSim cycle count is therefore T_local_baseline.
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_root=""
image="chipsystemsim:native-matmul-dp-counterfactual-v1"
prefix="matmul-counterfactual"
per_node_cpus=8
per_node_memory="16g"
delay="1ms"

usage() {
  cat <<'EOF'
Usage: run_matmul_dp_counterfactual_baseline.sh --output-root DIR [options]

Run 1/2/4/8 logical-worker Matmul-DP local-network counterfactuals on an
eight-node single-host DinD Swarm. Results are cycle-domain baselines for a
matching actual ns-3 timing run; they are not Docker wall-clock baselines.

Options:
  --image IMAGE              Built counterfactual runtime image.
  --prefix NAME              Dedicated DinD container prefix.
  --per-node-cpus N          CPU quota for every DinD node (default: 8).
  --per-node-memory SIZE     Memory limit for every DinD node (default: 16g).
  --delay DURATION           Functional PipeComm netem one-way delay (default: 1ms).
EOF
}

while (($#)); do
  case "$1" in
    --output-root) output_root="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --per-node-cpus) per_node_cpus="$2"; shift 2 ;;
    --per-node-memory) per_node_memory="$2"; shift 2 ;;
    --delay) delay="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$output_root" ]] || { usage >&2; exit 2; }
[[ ! -e "$output_root" ]] || { echo "refusing to overwrite: $output_root" >&2; exit 2; }
docker image inspect "$image" >/dev/null

mkdir -p "$output_root/config" "$output_root/results" "$output_root/dind-storage"

cleanup() {
  "$project_root/scripts/provision_dind_swarm.sh" --nodes 8 --prefix "$prefix" \
    --dind-data-root "$output_root/dind-storage" --cleanup || true
}
trap cleanup EXIT

python3 "$project_root/scripts/generate_matmul_dp_dind_matrix.py" \
  --output-root "$output_root/config" --image "$image" --nodes 1 2 4 8 --gpu-ranks 32 \
  --ns3-localize-cross-worker-network

"$project_root/scripts/provision_dind_swarm.sh" \
  --nodes 8 --dind-image chipsystemsim:dind-netem --image "$image" \
  --per-node-cpus "$per_node_cpus" --per-node-memory "$per_node_memory" \
  --delay "$delay" --rate none --image-load-jobs 4 \
  --dind-data-root "$output_root/dind-storage" --prefix "$prefix"

manager="${prefix}-0"
for nodes in 1 2 4 8; do
  stack="matmulcounterfactualn${nodes}"
  result="$output_root/results/nodes${nodes}"
  printf 'START nodes=%s at=%s\n' "$nodes" "$(date -Is)"
  "$project_root/scripts/run_dind_mlp_dp.sh" \
    --nodes "$nodes" --prefix "$prefix" \
    --config-dir "$output_root/config/matmul-dp-nodes${nodes}" \
    --stack "$stack" --output-dir "$result" --timeout-seconds 1800
  python3 "$project_root/scripts/validate_matmul_dp_results.py" \
    --coordinator-log "$result/coordinator.log" --ranks 32 \
    --output "$result/matmul_dp_validation.txt"
  docker exec "$manager" docker stack rm "$stack" >/dev/null 2>&1 || true
  sleep 3
  printf 'DONE nodes=%s at=%s\n' "$nodes" "$(date -Is)"
done

python3 "$project_root/scripts/summarize_dind_mlp_dp.py" \
  --input-root "$output_root/results" --directory-pattern 'nodes{nodes}' \
  --nodes 1 2 4 8 --output "$output_root/counterfactual_summary.csv"

echo "COUNTERFACTUAL_BASELINE_SUCCESS=$output_root"
