#!/usr/bin/env python3
"""Make the upstream MLP training window finite and configurable.

The original application already sends its native ``-1`` termination records
after ``train`` returns.  This patch changes only the number of complete
training iterations, so every iteration still uses the original CPU, CUDA,
MNSIM, DSA, and PipeComm paths.
"""
from __future__ import annotations

from pathlib import Path


SOURCE = Path("/opt/legosim/artifact/MLP/mlp.cpp")
OLD = "    train(x_train, y_train, 1);\n"
NEW = """    const char* iteration_text = std::getenv("LEGOSIM_MLP_TRAIN_ITERATIONS");
    const int train_iterations = iteration_text == nullptr ? 1 : std::atoi(iteration_text);
    if (train_iterations < 1) {
        std::cerr << "LEGOSIM_MLP_TRAIN_ITERATIONS must be positive" << std::endl;
        return EXIT_FAILURE;
    }
    std::cout << "legosim: mlp_train_iterations=" << train_iterations << std::endl;
    train(x_train, y_train, train_iterations);
"""


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    if NEW in source:
        return
    if OLD not in source:
        raise RuntimeError(f"unexpected upstream MLP training call: {SOURCE}")
    SOURCE.write_text(source.replace(OLD, NEW, 1), encoding="utf-8")


if __name__ == "__main__":
    main()
