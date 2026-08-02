#!/usr/bin/env python3
"""Render the Chinese multi-machine panel from the calibrated MLP estimate."""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    data = pd.read_csv(arguments.input)
    if data["nodes"].tolist() != [1, 2, 4, 8]:
        raise ValueError("expected the 1, 2, 4, 8 machine estimate")
    if not (data["measurement_status"] == "model-estimate-not-measured").all():
        raise ValueError("input must remain explicitly marked as a model estimate")

    plt.rcParams.update(
        {
            "font.family": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "mathtext.fontset": "stix",
            "font.size": 12,
            "axes.labelsize": 13,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
            "legend.fontsize": 10,
            "axes.unicode_minus": False,
        }
    )

    positions = np.arange(len(data))
    overhead = data["modeled_sync_overhead_percent"].to_numpy()
    total_time = data["modeled_total_simulation_seconds"].to_numpy()

    figure, overhead_axis = plt.subplots(figsize=(4.2, 3.5))
    overhead_axis.bar(
        positions, overhead, width=0.48, color="#FAA36E", edgecolor="black",
        linewidth=1.0, label="同步开销", zorder=2,
    )
    overhead_axis.set_xlabel("物理机数量", fontweight="bold")
    overhead_axis.set_ylabel("同步开销 (%)", fontweight="bold")
    overhead_axis.set_xticks(positions, data["nodes"].to_numpy(), fontweight="bold")
    overhead_axis.set_ylim(0, 1.85)
    overhead_axis.grid(axis="y", linestyle="--", linewidth=0.8, alpha=0.5)
    overhead_axis.set_axisbelow(True)

    time_axis = overhead_axis.twinx()
    time_axis.plot(
        positions, total_time, color="#800026", marker="o", markersize=7,
        linewidth=2, label="总仿真时间", zorder=3,
    )
    time_axis.set_ylabel("总仿真时间 (s)", fontweight="bold")
    # Keep the same layout for both short and 100x modelled workloads.
    lower = max(0.0, float(total_time.min()) * 0.85)
    upper = float(total_time.max()) * 1.10
    time_axis.set_ylim(lower, upper)

    for index, value in enumerate(overhead):
        overhead_axis.annotate(
            f"{value:.2f}%", xy=(positions[index], value), xytext=(0, 4),
            textcoords="offset points", ha="center", va="bottom", fontsize=9,
            fontweight="bold",
        )
    for index, value in enumerate(total_time):
        time_axis.annotate(
            f"{value:.2f}", xy=(positions[index], value), xytext=(0, 6),
            textcoords="offset points", ha="center", va="bottom", fontsize=9,
            color="#E45756", fontweight="bold",
        )

    left_handles, _ = overhead_axis.get_legend_handles_labels()
    right_handles, _ = time_axis.get_legend_handles_labels()
    overhead_axis.legend(
        left_handles + right_handles, ["同步开销", "总仿真时间"], loc="upper center",
        bbox_to_anchor=(0.5, 1.22), ncol=2, frameon=False, columnspacing=0.8,
    )
    figure.subplots_adjust(left=0.17, right=0.83, bottom=0.16, top=0.78)

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(arguments.output, dpi=300, bbox_inches="tight")


if __name__ == "__main__":
    main()
