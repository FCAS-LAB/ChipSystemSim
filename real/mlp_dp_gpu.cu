// One deterministic GPU worker for the MLP-DP benchmark.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <chrono>
#include <iostream>
#include <vector>

#include "apis_cu.h"
#include "cuda_runtime.h"

namespace {
// Must match the CPU controller exactly. This wider MLP keeps the experiment
// compute-dominated without introducing synthetic busy-work.
constexpr int kFeatures = 16;
constexpr int kHidden = 128;
constexpr int kClasses = 3;
constexpr int kW1 = kFeatures * kHidden;
constexpr int kB1 = kHidden;
constexpr int kW2 = kHidden * kClasses;
constexpr int kB2 = kClasses;
constexpr int kParameters = kW1 + kB1 + kW2 + kB2;
constexpr int kMaximumRanks = 8;
constexpr int kMaximumSamples = 1 << 20;

struct WorkHeader { int64_t iteration; int64_t samples; };

int W1(int feature, int hidden) { return feature * kHidden + hidden; }
int B1(int hidden) { return kW1 + hidden; }
int W2(int hidden, int output) { return kW1 + kB1 + hidden * kClasses + output; }
int B2(int output) { return kW1 + kB1 + kW2 + output; }

int world_size() {
  const char* configured = std::getenv("LEGOSIM_MLP_DP_RANKS");
  if (configured == nullptr || *configured == '\0') return kMaximumRanks;
  const long value = std::strtol(configured, nullptr, 10);
  return value > 0 && value <= kMaximumRanks ? static_cast<int>(value) : -1;
}

// GPGPU-Sim 4 corrupts its CUDA-11 kernel-launch queue for this workload.
// Keep each logical GPU as a CUDA/GPGPU-Sim process and retain its device
// PipeComm buffers; perform the tiny deterministic arithmetic on the host.
// This is a functional compatibility path, not a GPU-cycle model.
void calculate_gradient(const std::vector<double>& model, int rank, int gpu_index, int ranks,
                        int samples, std::vector<double>& gradient) {
  std::fill(gradient.begin(), gradient.end(), 0.0);
  for (int local_sample = 0; local_sample < samples; ++local_sample) {
    const int sample = rank + ranks * (gpu_index + 2 * local_sample);
    const int label = sample % kClasses;
    double hidden[kHidden];
    double logits[kClasses];
    double probability[kClasses];
    for (int h = 0; h < kHidden; ++h) {
      double value = model[B1(h)];
      for (int f = 0; f < kFeatures; ++f) {
        const double feature = static_cast<double>(((sample + 3) * (f + 5)) % 17 - 8) / 8.0;
        value += feature * model[W1(f, h)];
      }
      hidden[h] = value > 0.0 ? value : 0.0;
    }
    double normalizer = 0.0;
    for (int output = 0; output < kClasses; ++output) {
      logits[output] = model[B2(output)];
      for (int h = 0; h < kHidden; ++h) logits[output] += hidden[h] * model[W2(h, output)];
      probability[output] = exp(logits[output]);
      normalizer += probability[output];
    }
    for (int output = 0; output < kClasses; ++output) probability[output] /= normalizer;
    for (int output = 0; output < kClasses; ++output) {
      const double error = probability[output] - (label == output ? 1.0 : 0.0);
      gradient[B2(output)] += error;
      for (int h = 0; h < kHidden; ++h) gradient[W2(h, output)] += hidden[h] * error;
    }
    for (int h = 0; h < kHidden; ++h) {
      double backprop = 0.0;
      for (int output = 0; output < kClasses; ++output) {
        const double error = probability[output] - (label == output ? 1.0 : 0.0);
        backprop += error * model[W2(h, output)];
      }
      if (hidden[h] == 0.0) backprop = 0.0;
      gradient[B1(h)] += backprop;
      for (int f = 0; f < kFeatures; ++f) {
        const double feature = static_cast<double>(((sample + 3) * (f + 5)) % 17 - 8) / 8.0;
        gradient[W1(f, h)] += feature * backprop;
      }
    }
  }
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) return 2;
  const int rank = std::atoi(argv[1]);
  const int gpu_y = std::atoi(argv[2]);
  const int ranks = world_size();
  if (rank < 0 || rank >= ranks || gpu_y < 1 || gpu_y > 2) return 2;
  long long compute_nanoseconds = 0;
  while (true) {
    WorkHeader header{};
    // GPGPU-Sim's PipeComm implementation writes received bytes to device
    // memory. Receive the small control record through a device buffer, then
    // copy it back for host-side loop control.
    WorkHeader* device_header = nullptr;
    cudaMalloc(&device_header, sizeof(WorkHeader));
    receiveMessage(rank, gpu_y, rank, 0, device_header, sizeof(WorkHeader));
    cudaMemcpy(&header, device_header, sizeof(WorkHeader), cudaMemcpyDeviceToHost);
    cudaFree(device_header);
    if (header.iteration < 0) {
      std::cout << "MLP_DP_GPU_COMPUTE_NS rank=" << rank << " gpu=" << gpu_y
                << " value=" << compute_nanoseconds << std::endl;
      return 0;
    }
    if (header.samples <= 0 || header.samples > kMaximumSamples) {
      std::cerr << "MLP_DP_INVALID_HEADER rank=" << rank << " gpu=" << gpu_y
                << " iteration=" << header.iteration
                << " samples=" << header.samples << std::endl;
      return 3;
    }
    double *model = nullptr, *gradient = nullptr;
    cudaMalloc(&model, kParameters * sizeof(double));
    cudaMalloc(&gradient, kParameters * sizeof(double));
    receiveMessage(rank, gpu_y, rank, 0, model, kParameters * sizeof(double));
    std::vector<double> host_model(kParameters);
    std::vector<double> host_gradient(kParameters);
    cudaMemcpy(host_model.data(), model, kParameters * sizeof(double), cudaMemcpyDeviceToHost);
    const auto compute_started = std::chrono::steady_clock::now();
    calculate_gradient(host_model, rank, gpu_y - 1, ranks, static_cast<int>(header.samples), host_gradient);
    compute_nanoseconds += std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - compute_started).count();
    cudaMemcpy(gradient, host_gradient.data(), kParameters * sizeof(double), cudaMemcpyHostToDevice);
    sendMessage(rank, 0, rank, gpu_y, gradient, kParameters * sizeof(double));
    cudaFree(model); cudaFree(gradient);
  }
}
