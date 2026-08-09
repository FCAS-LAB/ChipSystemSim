// One native GPGPU-Sim worker for a fixed-global-size block GEMM.
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <stdexcept>

#include "apis_cu.h"
#include "cuda_runtime.h"

namespace {
constexpr int kGlobalRows = 480;
constexpr int kInner = 64;
constexpr int kColumns = 64;
constexpr int kMaxRanks = 35;

struct WorkHeader {
  int64_t iteration;
  int64_t row_begin;
  int64_t rows;
};

__global__ void BlockGemm(const int64_t* a, const int64_t* b, int64_t* c, int rows) {
  const int column = threadIdx.x + blockDim.x * blockIdx.x;
  const int row = threadIdx.y + blockDim.y * blockIdx.y;
  if (row >= rows || column >= kColumns) return;
  int64_t sum = 0;
  for (int k = 0; k < kInner; ++k) sum += a[row * kInner + k] * b[k * kColumns + column];
  c[row * kColumns + column] = sum;
}

int ParsePositive(const char* text, const char* name) {
  try {
    const int value = std::stoi(text);
    if (value > 0) return value;
  } catch (const std::exception&) {
  }
  throw std::runtime_error(std::string(name) + " must be positive");
}
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 5) {
      std::cerr << "usage: matmul_dp_cu X Y RANK GPU_RANKS" << std::endl;
      return 2;
    }
    const int id_x = std::atoi(argv[1]);
    const int id_y = std::atoi(argv[2]);
    const int rank = std::atoi(argv[3]);
    const int ranks = ParsePositive(argv[4], "GPU_RANKS");
    if (rank < 0 || rank >= ranks || ranks > kMaxRanks || kGlobalRows % ranks != 0) {
      throw std::runtime_error("invalid rank configuration");
    }
    const int max_rows = kGlobalRows / ranks;
    int64_t *device_header = nullptr, *device_a = nullptr, *device_b = nullptr, *device_c = nullptr;
    cudaMalloc(&device_header, sizeof(WorkHeader));
    cudaMalloc(&device_a, static_cast<size_t>(max_rows) * kInner * sizeof(int64_t));
    cudaMalloc(&device_b, static_cast<size_t>(kInner) * kColumns * sizeof(int64_t));
    cudaMalloc(&device_c, static_cast<size_t>(max_rows) * kColumns * sizeof(int64_t));
    int64_t compute_ns = 0;

    while (true) {
      WorkHeader header{};
      receiveMessage(id_x, id_y, 5, 5, device_header, sizeof(header));
      cudaMemcpy(&header, device_header, sizeof(header), cudaMemcpyDeviceToHost);
      if (header.iteration < 0) break;
      if (header.rows <= 0 || header.rows > max_rows || header.row_begin < 0 ||
          header.row_begin + header.rows > kGlobalRows) {
        throw std::runtime_error("invalid controller work header");
      }
      receiveMessage(id_x, id_y, 5, 5, device_a,
                     static_cast<size_t>(header.rows) * kInner * sizeof(int64_t));
      receiveMessage(id_x, id_y, 5, 5, device_b,
                     static_cast<size_t>(kInner) * kColumns * sizeof(int64_t));
      const auto started = std::chrono::steady_clock::now();
      const dim3 threads(8, 8);
      const dim3 blocks((kColumns + threads.x - 1) / threads.x,
                        (static_cast<unsigned>(header.rows) + threads.y - 1) / threads.y);
      BlockGemm<<<blocks, threads>>>(device_a, device_b, device_c, static_cast<int>(header.rows));
      compute_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now() - started).count();
      sendMessage(5, 5, id_x, id_y, device_c,
                  static_cast<size_t>(header.rows) * kColumns * sizeof(int64_t));
    }
    cudaFree(device_header);
    cudaFree(device_a);
    cudaFree(device_b);
    cudaFree(device_c);
    std::cout << "MATMUL_DP_GPU_COMPUTE_NS rank=" << rank << " value=" << compute_ns << std::endl;
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "MATMUL_DP_GPU_ERROR " << error.what() << std::endl;
    return 1;
  }
}
