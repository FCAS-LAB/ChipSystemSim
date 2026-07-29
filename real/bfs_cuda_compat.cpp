// Compatibility execution body for the upstream BFS CUDA task.
//
// It preserves the original process name, coordinates and six inbound / one
// outbound InterChiplet payloads. The bundled GPGPU-Sim 4 runtime corrupts its
// kernel-config queue for this CUDA-11.5 binary before the kernel can run;
// this host implementation performs the same bounded BFS relaxation so the
// distributed workload graph remains executable and auditable.

#include <array>
#include <cstdlib>
#include <iostream>

#include "apis_cu.h"

namespace {
constexpr int kNodeCount = 6;
constexpr int kEdgeCount = 8;

struct Node {
    int starting;
    int edge_count;
};

void requireCudaSuccess(cudaError_t result, const char* operation) {
    if (result != cudaSuccess) {
        std::cerr << "BFS CUDA compatibility " << operation
                  << " failed with CUDA error " << static_cast<int>(result) << std::endl;
        std::exit(EXIT_FAILURE);
    }
}
}  // namespace

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: bfs_cu <chiplet-x> <chiplet-y>" << std::endl;
        return EXIT_FAILURE;
    }
    const int id_x = std::atoi(argv[1]);
    const int id_y = std::atoi(argv[2]);

    std::array<int, kNodeCount> starts{};
    std::array<int, kNodeCount> edge_counts{};
    std::array<int, kEdgeCount> edges{};
    std::array<bool, kNodeCount> frontier{};
    std::array<bool, kNodeCount> visited{};
    std::array<int, kNodeCount> costs{};

    // The CUDA InterChiplet API transfers through GPGPU-Sim device backing
    // storage, exactly as the upstream bfs.cu does. Do not pass host arrays
    // here: its PipeComm implementation dereferences simulated device memory.
    int *device_starts = nullptr, *device_edge_counts = nullptr, *device_edges = nullptr, *device_costs = nullptr;
    bool *device_frontier = nullptr, *device_visited = nullptr;
    requireCudaSuccess(cudaMalloc(reinterpret_cast<void**>(&device_starts), sizeof(starts)), "allocate starts");
    requireCudaSuccess(cudaMalloc(reinterpret_cast<void**>(&device_edge_counts), sizeof(edge_counts)), "allocate counts");
    requireCudaSuccess(cudaMalloc(reinterpret_cast<void**>(&device_edges), sizeof(edges)), "allocate edges");
    requireCudaSuccess(cudaMalloc(reinterpret_cast<void**>(&device_frontier), sizeof(frontier)), "allocate frontier");
    requireCudaSuccess(cudaMalloc(reinterpret_cast<void**>(&device_visited), sizeof(visited)), "allocate visited");
    requireCudaSuccess(cudaMalloc(reinterpret_cast<void**>(&device_costs), sizeof(costs)), "allocate costs");

    requireCudaSuccess(receiveMessage(id_x, id_y, 5, 5, device_starts, sizeof(starts)), "receive starts");
    requireCudaSuccess(receiveMessage(id_x, id_y, 5, 5, device_edge_counts, sizeof(edge_counts)), "receive counts");
    requireCudaSuccess(receiveMessage(id_x, id_y, 5, 5, device_edges, sizeof(edges)), "receive edges");
    requireCudaSuccess(receiveMessage(id_x, id_y, 5, 5, device_frontier, sizeof(frontier)), "receive frontier");
    requireCudaSuccess(receiveMessage(id_x, id_y, 5, 5, device_visited, sizeof(visited)), "receive visited");
    requireCudaSuccess(receiveMessage(id_x, id_y, 5, 5, device_costs, sizeof(costs)), "receive costs");
    requireCudaSuccess(cudaMemcpy(starts.data(), device_starts, sizeof(starts), cudaMemcpyDeviceToHost), "copy starts");
    requireCudaSuccess(cudaMemcpy(edge_counts.data(), device_edge_counts, sizeof(edge_counts), cudaMemcpyDeviceToHost), "copy counts");
    requireCudaSuccess(cudaMemcpy(edges.data(), device_edges, sizeof(edges), cudaMemcpyDeviceToHost), "copy edges");
    requireCudaSuccess(cudaMemcpy(frontier.data(), device_frontier, sizeof(frontier), cudaMemcpyDeviceToHost), "copy frontier");
    requireCudaSuccess(cudaMemcpy(visited.data(), device_visited, sizeof(visited), cudaMemcpyDeviceToHost), "copy visited");
    requireCudaSuccess(cudaMemcpy(costs.data(), device_costs, sizeof(costs), cudaMemcpyDeviceToHost), "copy costs");

    // The CPU participant supplies an initial frontier. A finite graph can
    // make progress in at most kNodeCount rounds; use next_frontier to avoid
    // the non-deterministic same-round races of the original CUDA kernel.
    for (int round = 0; round < kNodeCount; ++round) {
        std::array<bool, kNodeCount> next_frontier{};
        bool progressed = false;
        for (int node = 0; node < kNodeCount; ++node) {
            if (!frontier[node]) continue;
            frontier[node] = false;
            visited[node] = true;
            const int first = starts[node];
            const int last = first + edge_counts[node];
            for (int edge_index = first; edge_index < last; ++edge_index) {
                if (edge_index < 0 || edge_index >= kEdgeCount) continue;
                const int neighbour = edges[edge_index];
                if (neighbour < 0 || neighbour >= kNodeCount || visited[neighbour]) continue;
                costs[neighbour] = costs[node] + 1;
                next_frontier[neighbour] = true;
                progressed = true;
            }
        }
        frontier = next_frontier;
        if (!progressed) break;
    }

    std::cout << "BFS CUDA compatibility completed at " << id_x << ',' << id_y << std::endl;
    requireCudaSuccess(cudaMemcpy(device_costs, costs.data(), sizeof(costs), cudaMemcpyHostToDevice), "copy costs back");
    requireCudaSuccess(sendMessage(5, 5, id_x, id_y, device_costs, sizeof(costs)), "send costs");
    requireCudaSuccess(cudaFree(device_starts), "free starts");
    requireCudaSuccess(cudaFree(device_edge_counts), "free counts");
    requireCudaSuccess(cudaFree(device_edges), "free edges");
    requireCudaSuccess(cudaFree(device_frontier), "free frontier");
    requireCudaSuccess(cudaFree(device_visited), "free visited");
    requireCudaSuccess(cudaFree(device_costs), "free costs");
    return EXIT_SUCCESS;
}
