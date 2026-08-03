#!/usr/bin/env bash
# Run one fixed-per-node-resource MLP-DP point and retain only the steady phase.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
nodes="${1:?usage: $0 NODES OUTPUT_ROOT IMAGE_ARCHIVE IMAGE}"
out="${2:?usage: $0 NODES OUTPUT_ROOT IMAGE_ARCHIVE IMAGE}"
archive="${3:?usage: $0 NODES OUTPUT_ROOT IMAGE_ARCHIVE IMAGE}"
image="${4:?usage: $0 NODES OUTPUT_ROOT IMAGE_ARCHIVE IMAGE}"
# Keep physical Docker resources constant on every simulated machine while the
# number of machines scales.  The application topology remains one CPU rank
# and two GPU workers per node, as generated below.
per_node_cpus="${LEGOSIM_PER_NODE_CPUS:-8}"
per_node_memory_gib="${LEGOSIM_PER_NODE_MEMORY_GIB:-16}"
[[ "$nodes" =~ ^(1|2|4|8)$ && ! -e "$out" && -r "$archive" ]] || exit 2
[[ "$per_node_cpus" =~ ^[1-9][0-9]*$ && "$per_node_memory_gib" =~ ^[1-9][0-9]*$ ]] || exit 2
prefix="chipsystemsim-steady-n${nodes}"
mkdir -p "$out/config" "$out/dind-storage"
cleanup() { bash "$root/scripts/provision_dind_swarm.sh" --nodes "$nodes" --prefix "$prefix" --dind-data-root "$out/dind-storage" --cleanup || true; }
trap cleanup EXIT
python3 "$root/scripts/generate_mlp_dp_matrix.py" --output-root "$out/config" --image "$image" --nodes "$nodes" --iterations 3 --global-samples 32768 --ranks-per-node 1
total_cpus=$((nodes * per_node_cpus))
total_memory_gib=$((nodes * per_node_memory_gib))
bash "$root/scripts/provision_dind_swarm.sh" --nodes "$nodes" --dind-image chipsystemsim:dind-netem --image "$image" --image-archive "$archive" --total-cpus "$total_cpus" --total-memory "${total_memory_gib}g" --delay 1ms --rate none --image-load-jobs 4 --dind-data-root "$out/dind-storage" --prefix "$prefix"
for slot in $(seq 0 $((nodes - 1))); do
  first_cpu=$((slot * per_node_cpus))
  last_cpu=$((first_cpu + per_node_cpus - 1))
  docker update --cpus "$per_node_cpus" --memory "${per_node_memory_gib}g" --memory-swap "${per_node_memory_gib}g" --cpuset-cpus "${first_cpu}-${last_cpu}" "${prefix}-${slot}" >/dev/null
done
bash "$root/scripts/run_dind_mlp_dp.sh" --nodes "$nodes" --prefix "$prefix" --config-dir "$out/config/mlp-dp-nodes${nodes}" --stack "mlp_steady_n${nodes}" --output-dir "$out/measurement" --timeout-seconds 1600
echo "STEADY_ONCE_SUCCESS=$out"
