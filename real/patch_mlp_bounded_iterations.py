#!/usr/bin/env python3
"""Optionally bound the upstream MLP CUDA worker request loop.

The upstream CPU participant sends several matrix-multiplication requests and
then a ``-1`` sentinel to every CUDA worker.  That sentinel is the production
termination protocol and must remain the default.  For a focused diagnostic,
``LEGOSIM_MLP_MAX_ITERATIONS`` may be set to a positive value to stop after a
known number of completed requests.  The patch also releases per-request host
and device allocations that the original loop retained until process exit.
"""
from __future__ import annotations

from pathlib import Path


SOURCE = Path("/opt/legosim/artifact/MLP/mlp.cu")
OLD_INCLUDE = '#include <cstdlib> \n'
NEW_INCLUDE = '#include <cstdlib> \n'
OLD_LOOP = '    while (1) {\n'
NEW_LOOP = '''    // Preserve the upstream -1 sentinel protocol by default.  A positive
    // environment value is only for a bounded diagnostic execution.
    const char* iteration_text = std::getenv("LEGOSIM_MLP_MAX_ITERATIONS");
    const int max_iterations = iteration_text == nullptr ? 0 : std::atoi(iteration_text);
    for (int iteration = 0; max_iterations <= 0 || iteration < max_iterations; ++iteration) {
'''
OLD_TAIL = '''        cudaFree(d_dataA);
        cudaFree(d_dataB);
        cudaFree(d_dataC);
    }
'''
NEW_TAIL = '''        cudaFree(d_dataA);
        cudaFree(d_dataB);
        cudaFree(d_dataC);
        cudaFree(Size_A);
        cudaFree(Size_B);
        delete[] size_A;
        delete[] size_B;
        free(A);
        free(C);
    }
'''


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    if NEW_LOOP in source:
        return
    if OLD_INCLUDE not in source or OLD_LOOP not in source or OLD_TAIL not in source:
        raise RuntimeError(f"unexpected upstream MLP CUDA worker layout: {SOURCE}")
    source = source.replace(OLD_INCLUDE, NEW_INCLUDE, 1)
    source = source.replace(OLD_LOOP, NEW_LOOP, 1)
    source = source.replace(OLD_TAIL, NEW_TAIL, 1)
    SOURCE.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
