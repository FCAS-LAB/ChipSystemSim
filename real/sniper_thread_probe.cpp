// Minimal reproduction for Sniper/Pin thread recording.
//
// DLRM creates eight std::thread workers after entering its GPU-dispatch
// phase. This probe removes all LEGOSim and CUDA code so a failure under
// run-sniper is attributable to the recorder's pthread handling rather than
// the distributed transport.

#include <atomic>
#include <cstdlib>
#include <iostream>
#include <thread>
#include <vector>

namespace {

std::atomic<int> started{0};
std::atomic<int> completed{0};

void Worker(int index) {
  started.fetch_add(1, std::memory_order_relaxed);

  // Keep a small, observable instruction body inside each worker. Volatile
  // prevents the compiler from replacing the loop with a constant result.
  volatile unsigned long long checksum = static_cast<unsigned long long>(index + 1);
  for (unsigned long long iteration = 0; iteration < 1000ULL; ++iteration) {
    checksum = checksum * 1664525ULL + 1013904223ULL;
  }
  if (checksum == 0) {
    std::cerr << "unreachable checksum" << std::endl;
  }

  completed.fetch_add(1, std::memory_order_relaxed);
}

}  // namespace

int main(int argc, char* argv[]) {
  const int worker_count = argc > 1 ? std::atoi(argv[1]) : 8;
  if (worker_count < 1) {
    std::cerr << "worker count must be positive" << std::endl;
    return 2;
  }

  std::cout << "probe: creating " << worker_count << " workers" << std::endl;
  std::vector<std::thread> workers;
  workers.reserve(static_cast<size_t>(worker_count));
  for (int index = 0; index < worker_count; ++index) {
    workers.emplace_back(Worker, index);
  }
  for (std::thread& worker : workers) {
    worker.join();
  }

  std::cout << "probe: started=" << started.load()
            << " completed=" << completed.load() << std::endl;
  return completed.load() == worker_count ? 0 : 1;
}
