#!/usr/bin/env python3
"""Restore the upstream MLP's independent GPU task parallelism.

The original source contains the threaded implementation as comments.  Keep a
runtime switch so serial execution remains available for an A/B control.
"""
from __future__ import annotations

from pathlib import Path


SOURCE = Path("/opt/legosim/artifact/MLP/mlp.cpp")
OLD_TOGGLER = """    std::vector<std::thread> THREAD;
    std::vector<std::vector<std::vector<double>>> res;
"""
NEW_TOGGLER = """    const char* parallel_text = std::getenv("LEGOSIM_MLP_PARALLEL_GPU_TASKS");
    const bool parallel_gpu_tasks = parallel_text != nullptr && std::atoi(parallel_text) != 0;
    std::vector<std::thread> THREAD;
    std::vector<std::vector<std::vector<double>>> res;
"""
OLD_LAUNCH = """        GpuMultiply(Dev1,Dev2,dev1[i].size(),dev1[i][0].size(),dev2[i].size(),dev2[i][0].size(),std::ref(res[i]),dstX,i+1);
        // std::thread
        // t(&BPNeuralNetwork::GpuMultiply,this,Dev1,Dev2,dev1[i].size(),dev1[i][0].size(),dev2[i].size(),dev2[i][0].size(),std::ref(res[i]),dstX,i+1);
        // THREAD.push_back(std::thread(GpuMultiply, Dev1, Dev2, dev1[i].size(), dev1[i][0].size(),
        //                              dev2[i].size(), dev2[i][0].size(), std::ref(res[i]), dstX,
        //                              i + 1));
    }
    // for (auto& i : THREAD) {
    //     i.join();
    // }
"""
NEW_LAUNCH = """        if (parallel_gpu_tasks) {
            THREAD.emplace_back(GpuMultiply, Dev1, Dev2, dev1[i].size(), dev1[i][0].size(),
                                dev2[i].size(), dev2[i][0].size(), std::ref(res[i]), dstX, i + 1);
        } else {
            GpuMultiply(Dev1, Dev2, dev1[i].size(), dev1[i][0].size(), dev2[i].size(),
                        dev2[i][0].size(), std::ref(res[i]), dstX, i + 1);
        }
    }
    for (auto& task : THREAD) {
        task.join();
    }
"""
OLD_BACKWARD = """        // std::thread t1(ToGPU, Weight, Dz, weight[i][0].size(), weight[i].size(), dz.size(),
        //                dz[0].size(), std::ref(deltas_pre), 1);
        // GpuMultiply(Weight,Dz,weight[i][0].size(),weight[i].size(),dz.size(),dz[0].size(),std::ref(deltas_pre),1);
        ToGPU(Weight, Dz, weight[i][0].size(), weight[i].size(), dz.size(),
                       dz[0].size(), std::ref(deltas_pre), 1);
        // std::thread t2(ToGPU, Dz, Activations_i, dz.size(), dz[0].size(), activations[i][0].size(),
        //                activations[i].size(), std::ref(dw), 2);
        ToGPU(Dz, Activations_i, dz.size(), dz[0].size(), activations[i][0].size(),
                       activations[i].size(), std::ref(dw), 2);
"""
NEW_BACKWARD = """        const char* parallel_text = std::getenv("LEGOSIM_MLP_PARALLEL_GPU_TASKS");
        const bool parallel_gpu_tasks = parallel_text != nullptr && std::atoi(parallel_text) != 0;
        if (parallel_gpu_tasks) {
            std::thread first(ToGPU, Weight, Dz, weight[i][0].size(), weight[i].size(), dz.size(),
                              dz[0].size(), std::ref(deltas_pre), 1);
            std::thread second(ToGPU, Dz, Activations_i, dz.size(), dz[0].size(),
                               activations[i][0].size(), activations[i].size(), std::ref(dw), 2);
            first.join();
            second.join();
        } else {
            ToGPU(Weight, Dz, weight[i][0].size(), weight[i].size(), dz.size(),
                  dz[0].size(), std::ref(deltas_pre), 1);
            ToGPU(Dz, Activations_i, dz.size(), dz[0].size(), activations[i][0].size(),
                  activations[i].size(), std::ref(dw), 2);
        }
"""


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    if NEW_TOGGLER in source and NEW_BACKWARD in source:
        return
    for old, new in ((OLD_TOGGLER, NEW_TOGGLER), (OLD_LAUNCH, NEW_LAUNCH), (OLD_BACKWARD, NEW_BACKWARD)):
        if old not in source:
            raise RuntimeError(f"unexpected upstream MLP block in {SOURCE}")
        source = source.replace(old, new, 1)
    SOURCE.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
