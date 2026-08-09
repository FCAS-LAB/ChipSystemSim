// Fixed-global-size block GEMM controller for distributed LEGOSim workers.
//
// The global 480x64 by 64x64 matrix stays fixed for every node-count point.
// Each GPU rank receives one contiguous row block, computes it with the
// native GPGPU-Sim executable, and returns the complete C block for exact
// validation.  Two GPU ranks are assigned to every logical worker.

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "apis_c.h"

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

struct Coordinate {
  int x;
  int y;
};

Coordinate GpuCoordinate(int rank) {
  // Reserve (5,5) for the Sniper controller.  The remaining 35 positions in
  // the 6x6 topology support a paper-scale fixed-simlet stress matrix.
  return {rank % 6, rank / 6};
}

int ParsePositive(const char* text, const char* name) {
  try {
    const int value = std::stoi(text);
    if (value > 0) return value;
  } catch (const std::exception&) {
  }
  throw std::runtime_error(std::string(name) + " must be positive");
}

void FillB(std::vector<int64_t>& matrix) {
  for (int k = 0; k < kInner; ++k) {
    for (int column = 0; column < kColumns; ++column) {
      // Values are independent of k so every expected C element has a simple,
      // exact closed form, while the GPU kernel still performs kInner products.
      matrix[k * kColumns + column] = column + 1;
    }
  }
}

void FillA(std::vector<int64_t>& matrix, int row_begin, int rows) {
  for (int row = 0; row < rows; ++row) {
    for (int k = 0; k < kInner; ++k) {
      matrix[row * kInner + k] = row_begin + row + 1;
    }
  }
}

void CheckBlock(const std::vector<int64_t>& matrix, int row_begin, int rows) {
  for (int row = 0; row < rows; ++row) {
    for (int column = 0; column < kColumns; ++column) {
      const int64_t expected = static_cast<int64_t>(kInner) *
          static_cast<int64_t>(row_begin + row + 1) * static_cast<int64_t>(column + 1);
      if (matrix[row * kColumns + column] != expected) {
        throw std::runtime_error("MATMUL_DP_VALIDATION_FAILED row=" +
                                 std::to_string(row_begin + row) + " column=" +
                                 std::to_string(column));
      }
    }
  }
}

int64_t Checksum(const std::vector<int64_t>& matrix) {
  int64_t total = 0;
  for (int64_t value : matrix) total += value;
  return total;
}

void SendBlock(int rank, int rows, int row_begin, int iteration,
               const std::vector<int64_t>& a, const std::vector<int64_t>& b) {
  const Coordinate coordinate = GpuCoordinate(rank);
  WorkHeader header{iteration, row_begin, rows};
  InterChiplet::sendMessage(coordinate.x, coordinate.y, 5, 5, &header, sizeof(header));
  InterChiplet::sendMessage(coordinate.x, coordinate.y, 5, 5,
                            const_cast<int64_t*>(a.data()), a.size() * sizeof(int64_t));
  InterChiplet::sendMessage(coordinate.x, coordinate.y, 5, 5,
                            const_cast<int64_t*>(b.data()), b.size() * sizeof(int64_t));
}

std::vector<int64_t> ReceiveBlock(int rank, int rows) {
  const Coordinate coordinate = GpuCoordinate(rank);
  std::vector<int64_t> result(static_cast<size_t>(rows) * kColumns);
  InterChiplet::receiveMessage(5, 5, coordinate.x, coordinate.y,
                               result.data(), result.size() * sizeof(int64_t));
  return result;
}

void StopWorkers(int ranks) {
  for (int rank = 0; rank < ranks; ++rank) {
    const Coordinate coordinate = GpuCoordinate(rank);
    WorkHeader stop{-1, 0, 0};
    InterChiplet::sendMessage(coordinate.x, coordinate.y, 5, 5, &stop, sizeof(stop));
  }
}
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 4) {
      std::cerr << "usage: matmul_dp_c X Y GPU_RANKS" << std::endl;
      return 2;
    }
    if (std::atoi(argv[1]) != 5 || std::atoi(argv[2]) != 5) {
      throw std::runtime_error("controller must run at chiplet (5,5)");
    }
    const int ranks = ParsePositive(argv[3], "GPU_RANKS");
    if (ranks > kMaxRanks || kGlobalRows % ranks != 0) {
      throw std::runtime_error("GPU_RANKS must divide 480 and be at most 35");
    }
    const int rows_per_rank = kGlobalRows / ranks;
    std::vector<int64_t> b(static_cast<size_t>(kInner) * kColumns);
    FillB(b);
    const auto started = std::chrono::steady_clock::now();

    // Dispatch all ranks before collecting any C block, exposing the complete
    // GPU compute window instead of serializing each block behind its result.
    for (int rank = 0; rank < ranks; ++rank) {
      std::vector<int64_t> a(static_cast<size_t>(rows_per_rank) * kInner);
      FillA(a, rank * rows_per_rank, rows_per_rank);
      SendBlock(rank, rows_per_rank, rank * rows_per_rank, 0, a, b);
    }

    int64_t checksum = 0;
    for (int rank = 0; rank < ranks; ++rank) {
      const std::vector<int64_t> result = ReceiveBlock(rank, rows_per_rank);
      CheckBlock(result, rank * rows_per_rank, rows_per_rank);
      checksum += Checksum(result);
    }
    const auto finished = std::chrono::steady_clock::now();
    StopWorkers(ranks);

    const int64_t steady_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        finished - started).count();
    std::ostringstream marker;
    marker << "MATMUL_DP_RESULT verification=ok ranks=" << ranks
           << " global_rows=" << kGlobalRows << " inner=" << kInner
           << " columns=" << kColumns << " checksum=" << checksum
           << " steady_ns=" << steady_ns << '\n';
    std::ofstream("matmul_dp_result.txt") << marker.str();
    std::cout << marker.str();
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "MATMUL_DP_ERROR " << error.what() << std::endl;
    return 1;
  }
}
