#!/usr/bin/env python3
"""Bound the BFS frontier loop for the legacy GPGPU-Sim runtime.

The upstream graph contains ``no_of_nodes`` vertices, so breadth-first
relaxation can make useful progress for at most that many rounds.  On the
shipped GPGPU-Sim 4 runtime, the device-side ``bool`` convergence flag can
remain set after the frontier is empty.  The original unbounded do/while loop
then launches the same kernel forever and eventually crashes the runtime.
"""
from __future__ import annotations

from pathlib import Path


SOURCE = Path("/opt/legosim/artifact/bfs_cuda/bfs.cu")
OLD = """    do {
        h_over = false;
        cudaMemcpy(d_over, &h_over, sizeof(bool), cudaMemcpyHostToDevice);
        bfsKernel<<<grid, block>>>(d_graph_nodes, d_graph_edges, d_graph_mask, d_graph_visited, d_cost, d_over, no_of_nodes);
        cudaDeviceSynchronize();
        cudaMemcpy(&h_over, d_over, sizeof(bool), cudaMemcpyDeviceToHost);
    } while (h_over);
"""
NEW = """    // A shortest-path traversal over N vertices needs no more than N
    // frontier expansions.  This guard keeps the legacy GPGPU-Sim bool flag
    // from causing a non-converging host loop while preserving early exit for
    // the normal, already-converged case.
    for (int iteration = 0; iteration < no_of_nodes; ++iteration) {
        h_over = false;
        cudaMemcpy(d_over, &h_over, sizeof(bool), cudaMemcpyHostToDevice);
        bfsKernel<<<grid, block>>>(d_graph_nodes, d_graph_edges, d_graph_mask, d_graph_visited, d_cost, d_over, no_of_nodes);
        cudaDeviceSynchronize();
        cudaMemcpy(&h_over, d_over, sizeof(bool), cudaMemcpyDeviceToHost);
        if (!h_over) break;
    }
"""


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    if NEW in source:
        return
    if OLD not in source:
        raise RuntimeError(f"unexpected upstream BFS frontier loop: {SOURCE}")
    SOURCE.write_text(source.replace(OLD, NEW), encoding="utf-8")


if __name__ == "__main__":
    main()
