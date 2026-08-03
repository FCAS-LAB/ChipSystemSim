// Deterministic synchronous data-parallel two-layer MLP CPU rank.
//
// One, two, four, or eight CPU ranks may be present, with two GPU workers per
// rank. A rank owns a stable shard, gathers its two GPU gradients, and the
// ranks use a deterministic binary-tree collective to reduce gradients and
// broadcast the updated model.
#include <algorithm>
#include <array>
#include <cmath>
#include <chrono>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "apis_c.h"

namespace {
constexpr int kGpusPerRank = 2;
constexpr int kDefaultIterations = 100;
constexpr int kDefaultSamples = 128;
constexpr int kMaximumRanks = 8;
constexpr int kMaximumSamples = 1 << 20;
// A wider hidden layer makes local gradient arithmetic the dominant portion
// of the scaling experiment while the global batch and reduction protocol
// remain unchanged between node counts.
constexpr int kFeatures = 16;
constexpr int kHidden = 128;
constexpr int kClasses = 3;
constexpr int kW1 = kFeatures * kHidden;
constexpr int kB1 = kHidden;
constexpr int kW2 = kHidden * kClasses;
constexpr int kB2 = kClasses;
constexpr int kParameters = kW1 + kB1 + kW2 + kB2;
constexpr double kLearningRate = 0.05;

struct WorkHeader {
  int64_t iteration;
  int64_t samples;
};

int positive_environment_value(const char* name, int default_value, int maximum_value) {
  const char* configured = std::getenv(name);
  if (configured == nullptr || *configured == '\0') return default_value;
  try {
    const int value = std::stoi(configured);
    if (value > 0 && value <= maximum_value) return value;
  } catch (const std::exception&) {
  }
  throw std::runtime_error(std::string(name) + " must be an integer between 1 and " +
                           std::to_string(maximum_value));
}

int training_iterations() {
  return positive_environment_value("LEGOSIM_MLP_DP_ITERATIONS", kDefaultIterations, 100000);
}

int world_size() {
  return positive_environment_value("LEGOSIM_MLP_DP_RANKS", kMaximumRanks, kMaximumRanks);
}

int global_samples() {
  return positive_environment_value("LEGOSIM_MLP_DP_SAMPLES", kDefaultSamples, kMaximumSamples);
}

void initialise(std::vector<double>& model) {
  for (int index = 0; index < kParameters; ++index) {
    model[index] = 0.01 * static_cast<double>((index % 7) - 3);
  }
}

void send_to_gpu(int rank, int gpu_y, int iteration, int ranks, int samples_per_gpu,
                 const std::vector<double>& model) {
  WorkHeader header{iteration, samples_per_gpu};
  // Features and labels are deterministic functions of rank, GPU, and local
  // sample index. The GPU compatibility worker reconstructs the identical
  // shard locally, avoiding an artificial megabyte-scale device PipeComm copy
  // whose cost would otherwise hide the intended compute critical path.
  (void)ranks;
  InterChiplet::sendMessage(rank, gpu_y, rank, 0, &header, sizeof(header));
  InterChiplet::sendMessage(rank, gpu_y, rank, 0, const_cast<double*>(model.data()),
                            model.size() * sizeof(double));
}

std::vector<double> receive_gradient(int rank, int gpu_y) {
  std::vector<double> gradient(kParameters);
  InterChiplet::receiveMessage(rank, 0, rank, gpu_y, gradient.data(), gradient.size() * sizeof(double));
  return gradient;
}

void add_into(std::vector<double>& destination, const std::vector<double>& source) {
  for (int index = 0; index < kParameters; ++index) destination[index] += source[index];
}

bool is_power_of_two(int value) {
  return value > 0 && (value & (value - 1)) == 0;
}

void reduce_gradient_tree(int rank, int ranks, std::vector<double>& gradient) {
  // At each level, the rank at the start of a contiguous group receives a
  // partial gradient from the group's upper half. Every send/receive pair is
  // independent of other groups at that level, avoiding rank-0 serialization.
  for (int stride = 1; stride < ranks; stride *= 2) {
    const int position_in_group = rank % (2 * stride);
    if (position_in_group == stride) {
      const int parent = rank - stride;
      InterChiplet::sendMessage(parent, 0, rank, 0, gradient.data(),
                                gradient.size() * sizeof(double));
      return;
    }
    if (position_in_group == 0) {
      const int child = rank + stride;
      if (child < ranks) {
        std::vector<double> child_gradient(kParameters);
        InterChiplet::receiveMessage(rank, 0, child, 0, child_gradient.data(),
                                     child_gradient.size() * sizeof(double));
        add_into(gradient, child_gradient);
      }
    }
  }
}

void broadcast_model_tree(int rank, int ranks, std::vector<double>& model) {
  // Reverse the reduction tree. A rank receives the updated model before it
  // forwards it to its own subtree, so each level can progress in parallel.
  for (int stride = ranks / 2; stride >= 1; stride /= 2) {
    const int position_in_group = rank % (2 * stride);
    if (position_in_group == 0) {
      const int child = rank + stride;
      if (child < ranks) {
        InterChiplet::sendMessage(child, 0, rank, 0, model.data(),
                                  model.size() * sizeof(double));
      }
    } else if (position_in_group == stride) {
      const int parent = rank - stride;
      InterChiplet::receiveMessage(rank, 0, parent, 0, model.data(),
                                   model.size() * sizeof(double));
    }
  }
}

void synchronise_model(int rank, int ranks, int samples, std::vector<double>& gradient,
                       std::vector<double>& model) {
  reduce_gradient_tree(rank, ranks, gradient);
  if (rank == 0) {
    for (int index = 0; index < kParameters; ++index) {
      model[index] -= kLearningRate * gradient[index] / static_cast<double>(samples);
    }
  }
  broadcast_model_tree(rank, ranks, model);
}

void stop_gpu_workers(int rank) {
  const WorkHeader stop{-1, 0};
  for (int gpu_y = 1; gpu_y <= kGpusPerRank; ++gpu_y) {
    InterChiplet::sendMessage(rank, gpu_y, rank, 0, const_cast<WorkHeader*>(&stop), sizeof(stop));
  }
}

void print_result(int rank, int iterations, int64_t steady_nanoseconds,
                  const std::vector<double>& model) {
  std::ostringstream result;
  result << "MLP_DP_RESULT rank=" << rank << " iterations=" << iterations
         << " steady_ns=" << steady_nanoseconds
         << " parameters=" << kParameters << " values=" << std::setprecision(17);
  for (double value : model) result << value << ',';
  result << '\n';
  // Sniper writes the application's stdout to sim.out. Persist the same
  // marker in the worker-local directory so the distributed worker can return
  // it through its proxy channel after Sniper exits.
  std::ofstream("mlp_dp_result.txt") << result.str();
  std::cout << result.str();
}

void start_measurement_barrier(int rank, int ranks) {
  const int token = 1;
  if (rank == 0) {
    for (int peer = 1; peer < ranks; ++peer) {
      int ready = 0;
      InterChiplet::receiveMessage(0, 0, peer, 0, &ready, sizeof(ready));
    }
    for (int peer = 1; peer < ranks; ++peer) {
      InterChiplet::sendMessage(peer, 0, 0, 0, const_cast<int*>(&token), sizeof(token));
    }
  } else {
    InterChiplet::sendMessage(0, 0, rank, 0, const_cast<int*>(&token), sizeof(token));
    int release = 0;
    InterChiplet::receiveMessage(rank, 0, 0, 0, &release, sizeof(release));
  }
}

void finish_measurement_barrier(int rank, int ranks) {
  const int token = 1;
  if (rank != 0) {
    InterChiplet::sendMessage(0, 0, rank, 0, const_cast<int*>(&token), sizeof(token));
    return;
  }
  for (int peer = 1; peer < ranks; ++peer) {
    int done = 0;
    InterChiplet::receiveMessage(0, 0, peer, 0, &done, sizeof(done));
  }
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) {
    std::cerr << "usage: mlp_dp_cpu RANK_X 0" << std::endl;
    return 2;
  }
  const int rank = std::stoi(argv[1]);
  const int y = std::stoi(argv[2]);
  const int ranks = world_size();
  const int samples = global_samples();
  if (!is_power_of_two(ranks)) {
    std::cerr << "LEGOSIM_MLP_DP_RANKS must be a power of two" << std::endl;
    return 2;
  }
  if (samples % (ranks * kGpusPerRank) != 0) {
    std::cerr << "global sample count must divide evenly across CPU/GPU ranks" << std::endl;
    return 2;
  }
  if (rank < 0 || rank >= ranks || y != 0) {
    std::cerr << "invalid CPU coordinate" << std::endl;
    return 2;
  }
  std::vector<double> model(kParameters);
  initialise(model);
  const int iterations = training_iterations();
  const int samples_per_gpu = samples / (ranks * kGpusPerRank);
  // All rank processes have already completed their simulator start-up when
  // they reach this barrier. The measured interval therefore isolates the
  // synchronous MLP work from process/container initialization and teardown.
  start_measurement_barrier(rank, ranks);
  const auto steady_started = std::chrono::steady_clock::now();
  for (int iteration = 0; iteration < iterations; ++iteration) {
    send_to_gpu(rank, 1, iteration, ranks, samples_per_gpu, model);
    send_to_gpu(rank, 2, iteration, ranks, samples_per_gpu, model);
    std::vector<double> gradient = receive_gradient(rank, 1);
    add_into(gradient, receive_gradient(rank, 2));
    synchronise_model(rank, ranks, samples, gradient, model);
  }
  finish_measurement_barrier(rank, ranks);
  const int64_t steady_nanoseconds = rank == 0
      ? std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - steady_started).count()
      : -1;
  stop_gpu_workers(rank);
  print_result(rank, iterations, steady_nanoseconds, model);
  return 0;
}
