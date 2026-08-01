// One deterministic GPU worker for the MLP-DP benchmark.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <vector>

#include "apis_cu.h"
#include "cuda_runtime.h"

namespace {
constexpr int kFeatures = 4;
constexpr int kHidden = 6;
constexpr int kClasses = 3;
constexpr int kW1 = kFeatures * kHidden;
constexpr int kB1 = kHidden;
constexpr int kW2 = kHidden * kClasses;
constexpr int kB2 = kClasses;
constexpr int kParameters = kW1 + kB1 + kW2 + kB2;

struct WorkHeader { int64_t iteration; int64_t samples; };

int W1(int feature, int hidden) { return feature * kHidden + hidden; }
int B1(int hidden) { return kW1 + hidden; }
int W2(int hidden, int output) { return kW1 + kB1 + hidden * kClasses + output; }
int B2(int output) { return kW1 + kB1 + kW2 + output; }

// GPGPU-Sim 4 corrupts its CUDA-11 kernel-launch queue for this workload.
// Keep each logical GPU as a CUDA/GPGPU-Sim process and retain its device
// PipeComm buffers; perform the tiny deterministic arithmetic on the host.
// This is a functional compatibility path, not a GPU-cycle model.
void calculate_gradient(const std::vector<double>& model, const std::vector<double>& features,
                        const std::vector<double>& labels, int samples,
                        std::vector<double>& gradient) {
  std::fill(gradient.begin(), gradient.end(), 0.0);
  for (int sample = 0; sample < samples; ++sample) {
    double hidden[kHidden];
    double logits[kClasses];
    double probability[kClasses];
    for (int h = 0; h < kHidden; ++h) {
      double value = model[B1(h)];
      for (int f = 0; f < kFeatures; ++f) value += features[sample * kFeatures + f] * model[W1(f, h)];
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
      const double error = probability[output] - (static_cast<int>(labels[sample]) == output ? 1.0 : 0.0);
      gradient[B2(output)] += error;
      for (int h = 0; h < kHidden; ++h) gradient[W2(h, output)] += hidden[h] * error;
    }
    for (int h = 0; h < kHidden; ++h) {
      double backprop = 0.0;
      for (int output = 0; output < kClasses; ++output) {
        const double error = probability[output] - (static_cast<int>(labels[sample]) == output ? 1.0 : 0.0);
        backprop += error * model[W2(h, output)];
      }
      if (hidden[h] == 0.0) backprop = 0.0;
      gradient[B1(h)] += backprop;
      for (int f = 0; f < kFeatures; ++f) gradient[W1(f, h)] += features[sample * kFeatures + f] * backprop;
    }
  }
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) return 2;
  const int rank = std::atoi(argv[1]);
  const int gpu_y = std::atoi(argv[2]);
  if (rank < 0 || rank >= 8 || gpu_y < 1 || gpu_y > 2) return 2;
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
    if (header.iteration < 0) return 0;
    if (header.samples <= 0 || header.samples > 128) {
      std::cerr << "MLP_DP_INVALID_HEADER rank=" << rank << " gpu=" << gpu_y
                << " iteration=" << header.iteration
                << " samples=" << header.samples << std::endl;
      return 3;
    }
    const size_t feature_count = static_cast<size_t>(header.samples) * kFeatures;
    double *model = nullptr, *features = nullptr, *labels = nullptr, *gradient = nullptr;
    cudaMalloc(&model, kParameters * sizeof(double));
    cudaMalloc(&features, feature_count * sizeof(double));
    cudaMalloc(&labels, header.samples * sizeof(double));
    cudaMalloc(&gradient, kParameters * sizeof(double));
    receiveMessage(rank, gpu_y, rank, 0, model, kParameters * sizeof(double));
    receiveMessage(rank, gpu_y, rank, 0, features, feature_count * sizeof(double));
    receiveMessage(rank, gpu_y, rank, 0, labels, header.samples * sizeof(double));
    std::vector<double> host_model(kParameters);
    std::vector<double> host_features(feature_count);
    std::vector<double> host_labels(header.samples);
    std::vector<double> host_gradient(kParameters);
    cudaMemcpy(host_model.data(), model, kParameters * sizeof(double), cudaMemcpyDeviceToHost);
    cudaMemcpy(host_features.data(), features, feature_count * sizeof(double), cudaMemcpyDeviceToHost);
    cudaMemcpy(host_labels.data(), labels, header.samples * sizeof(double), cudaMemcpyDeviceToHost);
    calculate_gradient(host_model, host_features, host_labels, static_cast<int>(header.samples), host_gradient);
    cudaMemcpy(gradient, host_gradient.data(), kParameters * sizeof(double), cudaMemcpyHostToDevice);
    sendMessage(rank, 0, rank, gpu_y, gradient, kParameters * sizeof(double));
    cudaFree(model); cudaFree(features); cudaFree(labels); cudaFree(gradient);
  }
}
