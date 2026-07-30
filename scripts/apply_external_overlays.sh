#!/usr/bin/env bash
# Apply the minimal out-of-tree changes required by the native integration.
# Run this from any directory after cloning the three sibling repositories.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LEGOSIM_ROOT="${1:-${PROJECT_ROOT}/../LEGOSIM_MICRO}"
BENCHMARK_ROOT="${2:-${PROJECT_ROOT}/../single_stage_simulator}"

apply_patch() {
  local repository="$1"
  local patch_file="$2"
  if [[ ! -d "${repository}/.git" ]]; then
    echo "Not a Git repository: ${repository}" >&2
    exit 1
  fi
  git -C "${repository}" apply --check "${patch_file}"
  git -C "${repository}" apply "${patch_file}"
}

apply_patch "${LEGOSIM_ROOT}" "${PROJECT_ROOT}/patches/external/LEGOSIM_MICRO-interchiplet-bridge.patch"
apply_patch "${BENCHMARK_ROOT}" "${PROJECT_ROOT}/patches/external/single_stage_simulator-moe.patch"

echo "External overlays applied. Review with:"
echo "  git -C ${LEGOSIM_ROOT} diff -- interchiplet/srcs/interchiplet.cpp"
echo "  git -C ${BENCHMARK_ROOT} diff -- benchmark/MoE/dram.cpp benchmark/MoE/moe.yml"
