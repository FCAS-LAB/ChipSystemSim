#!/usr/bin/env bash
# Build the unmodified upstream LEGOSim components required by the native runs.
# This script deliberately fails on a patch/build error: a partial build must
# never be mistaken for a usable simulation image.
set -euo pipefail

export SIMULATOR_ROOT=/opt/legosim
# Ubuntu's nvidia-cuda-toolkit installs nvcc and headers under /usr. The
# upstream GPGPU-Sim setup script dereferences this variable, so define it
# before sourcing while preserving an explicitly supplied CUDA installation.
export CUDA_INSTALL_PATH="${CUDA_INSTALL_PATH:-/usr}"
cd "$SIMULATOR_ROOT"
# The upstream setup script reads several optional variables without defaults.
# Keep strict mode for this build, but source that legacy script with nounset
# temporarily disabled.
set +u
source "$SIMULATOR_ROOT/gpgpu-sim/setup_environment"
set -u

./apply_patch.sh

cmake -S interchiplet -B interchiplet/build
cmake --build interchiplet/build --parallel
cmake -S popnet_chiplet -B popnet_chiplet/build
cmake --build popnet_chiplet/build --parallel
make -C snipersim -j"$(nproc)"
make -C gpgpu-sim -j"$(nproc)"

# CUDA 11.5 cannot compile these CUDA sources through Ubuntu 22.04's GCC 11
# C++ standard library.  Keep the system compiler for non-CUDA components and
# inject GCC 10 into each upstream Makefile's NVCC variable explicitly. Those
# Makefiles assign NVCC themselves, so CUDAHOSTCXX alone is not sufficient.
NVCC_WITH_GCC10="nvcc -ccbin /usr/bin/g++-10"

# The upstream MLP Makefile creates its json dependency inside a recipe but
# does not make compilation depend on that recipe.  A parallel invocation can
# compile mlp.cpp before json.hpp exists, so retain upstream behavior but run
# that one target serially.
make -C artifact/MLP NVCC="$NVCC_WITH_GCC10"

for benchmark in benchmark/resnet artifact/bfs_cuda; do
  make -C "$benchmark" NVCC="$NVCC_WITH_GCC10" -j"$(nproc)"
done

make -C benchmark/dlrm_npu -j"$(nproc)"
