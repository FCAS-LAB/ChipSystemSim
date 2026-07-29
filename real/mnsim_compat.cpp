// Compatibility MNSIM task for native LEGOSim benchmarks.
//
// This keeps the original PipeComm protocol and uses the checked-in upstream
// MNSIM result as an explicitly audited timing calibration. It does not claim
// to reproduce the unpublished MNSIM fork expected by the original scripts.

#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <unistd.h>

#include "apis_c.h"
#include "../../interchiplet/includes/pipe_comm.h"

namespace {
constexpr int kElements = 12 * 12 * 8;
constexpr int kElementBytes = sizeof(int64_t);

std::string shellQuote(const std::string& value) {
    return "'" + value + "'";
}

long long runCompatibilityModel(const char* workload, int id_x, int id_y) {
    const std::string stem = "/tmp/legosim-mnsim-" + std::to_string(getpid());
    const std::string result_path = stem + ".res";
    const std::string audit_path = stem + ".json";
    const std::string command =
        "python3 /opt/legosim-distributed/mnsim_compat.py --workload " +
        std::string(workload) + " --id1 " + std::to_string(id_x) + " --id2 " +
        std::to_string(id_y) + " --payload-elements " + std::to_string(kElements) +
        " --element-bytes " + std::to_string(kElementBytes) + " --output " +
        shellQuote(result_path) + " --audit " + shellQuote(audit_path);
    if (std::system(command.c_str()) != 0) {
        std::cerr << "MNSIM compatibility backend failed." << std::endl;
        std::exit(EXIT_FAILURE);
    }

    std::ifstream result(result_path);
    long long cycles = 0;
    if (!(result >> cycles) || cycles <= 0) {
        std::cerr << "MNSIM compatibility backend returned invalid cycles." << std::endl;
        std::exit(EXIT_FAILURE);
    }
    std::cout << "MNSIM compatibility cycles=" << cycles << " audit=" << audit_path << std::endl;
    return cycles;
}
}  // namespace

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: mnsim <chiplet-x> <chiplet-y>" << std::endl;
        return EXIT_FAILURE;
    }
    const int id_x = std::atoi(argv[1]);
    const int id_y = std::atoi(argv[2]);
    const char* workload = std::getenv("LEGOSIM_MNSIM_WORKLOAD");
    if (workload == nullptr) workload = "mlp";

    InterChiplet::PipeComm pipe_comm;
    int64_t input[kElements] = {};
    const std::string inbound = InterChiplet::receiveSync(5, 5, id_x, id_y);
    pipe_comm.read_data(inbound.c_str(), input, sizeof(input));
    const long long receive_end =
        InterChiplet::readSync(1, 5, 5, id_x, id_y, sizeof(input), 0);

    const long long model_cycles = runCompatibilityModel(workload, id_x, id_y);
    int64_t output[kElements] = {};
    for (int index = 0; index < kElements; ++index) output[index] = index;

    const std::string outbound = InterChiplet::sendSync(id_x, id_y, 5, 5);
    pipe_comm.write_data(outbound.c_str(), output, sizeof(output));
    InterChiplet::writeSync(receive_end + model_cycles, id_x, id_y, 5, 5, sizeof(output), 0);
    return EXIT_SUCCESS;
}
