#!/usr/bin/env bash
# Fixed-core 8-node MLP-DP warm-up plus three consecutive measurements.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out="${1:?usage: $0 OUTPUT_ROOT IMAGE_ARCHIVE [IMAGE]}"; archive="${2:?usage: $0 OUTPUT_ROOT IMAGE_ARCHIVE [IMAGE]}"
prefix="chipsystemsim-pinned-8"; image="${3:-chipsystemsim:mlp-dp-steady-v1}"
[[ ! -e "$out" && -r "$archive" ]] || exit 2
mkdir -p "$out/config" "$out/dind-storage"
cleanup() { "$root/scripts/provision_dind_swarm.sh" --nodes 8 --prefix "$prefix" --dind-data-root "$out/dind-storage" --cleanup || true; }
trap cleanup EXIT
python3 "$root/scripts/generate_mlp_dp_matrix.py" --output-root "$out/config" --image "$image" --nodes 8 --iterations 3 --global-samples 32768 --ranks-per-node 1
"$root/scripts/provision_dind_swarm.sh" --nodes 8 --dind-image chipsystemsim:dind-netem --image "$image" --image-archive "$archive" --total-cpus 64 --total-memory 128g --delay 1ms --rate none --image-load-jobs 4 --dind-data-root "$out/dind-storage" --prefix "$prefix"
for slot in $(seq 0 7); do docker update --cpus 8 --memory 16g --memory-swap 16g --cpuset-cpus "$((slot * 8))-$(((slot + 1) * 8 - 1))" "${prefix}-${slot}" >/dev/null; done
run() { "$root/scripts/run_dind_mlp_dp.sh" --nodes 8 --prefix "$prefix" --config-dir "$out/config/mlp-dp-nodes8" --stack "mlp_pinned8_$1" --output-dir "$out/$2" --timeout-seconds 1600; }
run once measurement
echo "PINNED8_SUCCESS=$out"
