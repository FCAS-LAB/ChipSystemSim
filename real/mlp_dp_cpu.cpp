// Deterministic synchronous data-parallel two-layer MLP CPU rank.
//
// Four CPU ranks and two GPU workers per rank are always present.  A rank owns
// a stable 1/4 input shard, gathers its two GPU gradients, and rank 0 reduces
// rank gradients in ascending-rank order before broadcasting the new model.
#include <algorithm>
#include <array>
#include <cmath>
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
constexpr int kRanks = 4;
constexpr int kGpusPerRank = 2;
constexpr int kIterations = 100;
constexpr int kSamples = 128;
constexpr int kFeatures = 4;
constexpr int kHidden = 6;
constexpr int kClasses = 3;
constexpr int kSamplesPerGpu = kSamples / (kRanks * kGpusPerRank);
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

void initialise(std::vector<double>& model) {
  for (int index = 0; index < kParameters; ++index) {
    model[index] = 0.01 * static_cast<double>((index % 7) - 3);
  }
}

void make_gpu_shard(int rank, int gpu, std::vector<double>& features,
                    std::vector<double>& labels) {
  features.resize(kSamplesPerGpu * kFeatures);
  labels.resize(kSamplesPerGpu);
  for (int local = 0; local < kSamplesPerGpu; ++local) {
    const int sample = rank + kRanks * (gpu + kGpusPerRank * local);
    labels[local] = static_cast<double>(sample % kClasses);
    for (int feature = 0; feature < kFeatures; ++feature) {
      // Fixed data makes every node-count experiment consume exactly the same
      // ordered global batch without depending on a local random generator.
      features[local * kFeatures + feature] =
          static_cast<double>(((sample + 3) * (feature + 5)) % 17 - 8) / 8.0;
    }
  }
}

void send_to_gpu(int rank, int gpu_y, int iteration, const std::vector<double>& model) {
  WorkHeader header{iteration, kSamplesPerGpu};
  std::vector<double> features;
  std::vector<double> labels;
  make_gpu_shard(rank, gpu_y - 1, features, labels);
  InterChiplet::sendMessage(rank, gpu_y, rank, 0, &header, sizeof(header));
  InterChiplet::sendMessage(rank, gpu_y, rank, 0, const_cast<double*>(model.data()),
                            model.size() * sizeof(double));
  InterChiplet::sendMessage(rank, gpu_y, rank, 0, features.data(), features.size() * sizeof(double));
  InterChiplet::sendMessage(rank, gpu_y, rank, 0, labels.data(), labels.size() * sizeof(double));
}

std::vector<double> receive_gradient(int rank, int gpu_y) {
  std::vector<double> gradient(kParameters);
  InterChiplet::receiveMessage(rank, 0, rank, gpu_y, gradient.data(), gradient.size() * sizeof(double));
  return gradient;
}

void add_into(std::vector<double>& destination, const std::vector<double>& source) {
  for (int index = 0; index < kParameters; ++index) destination[index] += source[index];
}

void synchronise_model(int rank, std::vector<double>& gradient, std::vector<double>& model) {
  if (rank == 0) {
    // This order is part of the correctness contract. It is unchanged by the
    // number of Swarm nodes, which keeps floating-point accumulation stable.
    for (int peer = 1; peer < kRanks; ++peer) {
      std::vector<double> peer_gradient(kParameters);
      InterChiplet::receiveMessage(0, 0, peer, 0, peer_gradient.data(),
                                   peer_gradient.size() * sizeof(double));
      add_into(gradient, peer_gradient);
    }
    for (int index = 0; index < kParameters; ++index) {
      model[index] -= kLearningRate * gradient[index] / static_cast<double>(kSamples);
    }
    for (int peer = 1; peer < kRanks; ++peer) {
      InterChiplet::sendMessage(peer, 0, 0, 0, model.data(), model.size() * sizeof(double));
    }
    return;
  }
  InterChiplet::sendMessage(0, 0, rank, 0, gradient.data(), gradient.size() * sizeof(double));
  InterChiplet::receiveMessage(rank, 0, 0, 0, model.data(), model.size() * sizeof(double));
}

void stop_gpu_workers(int rank) {
  const WorkHeader stop{-1, 0};
  for (int gpu_y = 1; gpu_y <= kGpusPerRank; ++gpu_y) {
    InterChiplet::sendMessage(rank, gpu_y, rank, 0, const_cast<WorkHeader*>(&stop), sizeof(stop));
  }
}

void print_result(int rank, const std::vector<double>& model) {
  std::ostringstream result;
  result << "MLP_DP_RESULT rank=" << rank << " iterations=" << kIterations
         << " parameters=" << kParameters << " values=" << std::setprecision(17);
  for (double value : model) result << value << ',';
  result << '\n';
  // Sniper writes the application's stdout to sim.out. Persist the same
  // marker in the worker-local directory so the distributed worker can return
  // it through its proxy channel after Sniper exits.
  std::ofstream("mlp_dp_result.txt") << result.str();
  std::cout << result.str();
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) {
    std::cerr << "usage: mlp_dp_cpu RANK_X 0" << std::endl;
    return 2;
  }
  const int rank = std::stoi(argv[1]);
  const int y = std::stoi(argv[2]);
  if (rank < 0 || rank >= kRanks || y != 0) {
    std::cerr << "invalid CPU coordinate" << std::endl;
    return 2;
  }
  std::vector<double> model(kParameters);
  initialise(model);
  for (int iteration = 0; iteration < kIterations; ++iteration) {
    send_to_gpu(rank, 1, iteration, model);
    send_to_gpu(rank, 2, iteration, model);
    std::vector<double> gradient = receive_gradient(rank, 1);
    add_into(gradient, receive_gradient(rank, 2));
    synchronise_model(rank, gradient, model);
  }
  stop_gpu_workers(rank);
  print_result(rank, model);
  return 0;
}
