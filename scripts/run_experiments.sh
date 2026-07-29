#!/usr/bin/env bash
# Run the complete Linux/Docker experiment matrix.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"

python3 "$project_root/scripts/run_experiments.py" "$@"
