#!/usr/bin/env bash
# Backwards-compatible generic name for the DinD natural-completion runner.
# The implementation accepts any generated native LEGOSim workload, not only
# MLP-DP, as long as it emits the standard coordinator completion marker.
set -euo pipefail
exec "$(dirname "$0")/run_dind_mlp_dp.sh" "$@"
