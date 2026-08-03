#!/usr/bin/env bash
# Run the reproducible MLP-DP DinD matrix with fixed resources per logical node.
#
# Each node executes one CPU rank and two GPU workers.  The global batch stays
# fixed, so increasing the node count data-parallelises one shared MLP task.
set -euo pipefail

image=""
output_root=""
nodes=(1 2 4 8)
image_archive=""
per_node_cpus=8
per_node_memory_gib=16

usage() {
  cat <<'EOF'
Usage: run_dind_mlp_dp_per_node_scaleout.sh --image IMAGE --output-root DIR [options]

Run the current MLP-DP matrix on 1, 2, 4, and 8 DinD Swarm nodes.  Every
logical node has one CPU rank, two GPU workers, 8 vCPU, and 16 GiB by default.

Options:
  --nodes "1 2 4 8"        Node counts to run (default: all four points).
  --image-archive FILE     Existing docker-save archive for IMAGE.
  --per-node-cpus N        Fixed vCPU quota per logical node (default: 8).
  --per-node-memory-gib N  Fixed memory limit per logical node (default: 16).
EOF
}

while (($#)); do
  case "$1" in
    --image) image="$2"; shift 2 ;;
    --output-root) output_root="$2"; shift 2 ;;
    --nodes) read -r -a nodes <<<"$2"; shift 2 ;;
    --image-archive) image_archive="$2"; shift 2 ;;
    --per-node-cpus) per_node_cpus="$2"; shift 2 ;;
    --per-node-memory-gib) per_node_memory_gib="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$image" && -n "$output_root" ]] || { usage >&2; exit 2; }
[[ "$per_node_cpus" =~ ^[1-9][0-9]*$ ]] || { echo "invalid --per-node-cpus" >&2; exit 2; }
[[ "$per_node_memory_gib" =~ ^[1-9][0-9]*$ ]] || { echo "invalid --per-node-memory-gib" >&2; exit 2; }
for node_count in "${nodes[@]}"; do
  [[ "$node_count" =~ ^(1|2|4|8)$ ]] || { echo "invalid node count: $node_count" >&2; exit 2; }
done

output_root=$(realpath -m "$output_root")
[[ ! -e "$output_root" ]] || { echo "refusing to overwrite $output_root" >&2; exit 2; }
mkdir -p "$output_root"

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
max_nodes=0
for node_count in "${nodes[@]}"; do
  if (( node_count > max_nodes )); then max_nodes="$node_count"; fi
done
required_cpus=$((max_nodes * per_node_cpus))
available_cpus=$(nproc)
(( available_cpus >= required_cpus )) || {
  echo "need at least $required_cpus host CPUs, found $available_cpus" >&2
  exit 2
}

owned_archive=""
cleanup() {
  [[ -z "$owned_archive" ]] || rm -f -- "$owned_archive"
}
trap cleanup EXIT INT TERM

if [[ -n "$image_archive" ]]; then
  [[ -r "$image_archive" ]] || { echo "cannot read image archive: $image_archive" >&2; exit 2; }
else
  docker image inspect "$image" >/dev/null
  image_archive="$output_root/${image//[^A-Za-z0-9_.-]/_}.tar"
  docker save --output "$image_archive" "$image"
  owned_archive="$image_archive"
fi

results=()
run_id=$(date +%Y%m%d-%H%M%S)
for node_count in "${nodes[@]}"; do
  result="$output_root/mlp-dp-steady-${node_count}-${run_id}"
  LEGOSIM_PER_NODE_CPUS="$per_node_cpus" \
  LEGOSIM_PER_NODE_MEMORY_GIB="$per_node_memory_gib" \
    "$root/scripts/run_dind_mlp_dp_steady_once.sh" "$node_count" "$result" "$image_archive" "$image"
  results+=("$result")
done

python3 "$root/scripts/collect_steady_matrix.py" \
  --output "$output_root/summary.csv" "${results[@]}"
printf 'completed fixed-per-node MLP-DP matrix: %s\n' "$output_root"
