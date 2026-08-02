#!/usr/bin/env python3
"""Render the physical-machine calibrated MLP estimate in sensitivity.py style.

The first panel is read from ``mlp_physical_machine_calibrated_estimate.csv``.
The two sensitivity panels deliberately retain the parameter-sweep values from
the user-provided sensitivity.py reference.  The output must be labelled as a
model estimate, never as a physical-machine measurement.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd


def format_ticks(value: float) -> str:
    """Format cycle-count tick labels using compact engineering suffixes."""
    if value >= 1e6:
        return f"{value / 1e6:g}M"
    if value >= 1e3:
        return f"{value / 1e3:g}k"
    return f"{value:g}"


def load_estimate(path: Path) -> pd.DataFrame:
    """Load and validate the explicitly modelled physical-machine scenario."""
    frame = pd.read_csv(path)
    expected_nodes = [1, 2, 4, 8]
    if frame["nodes"].tolist() != expected_nodes:
        raise ValueError(f"expected node sequence {expected_nodes}, got {frame['nodes'].tolist()}")
    if not (frame["measurement_status"] == "model-estimate-not-measured").all():
        raise ValueError("the input CSV must be explicitly labelled as a model estimate")
    return frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    estimate = load_estimate(arguments.input)

    # Preserve the formatting and sensitivity sweeps from sensitivity.py.
    profile = pd.DataFrame(
        {
            "profile_window": [10_000, 20_000, 40_000, 80_000, 160_000],
            "wall_seconds": [11_819_434_059, 11_824_366_802, 4_678_563_729,
                             5_461_024_707, 6_922_698_837],
        }
    )
    delta = pd.DataFrame(
        {
            "phase_cycles": [100_000, 200_000, 400_000, 800_000, 1_600_000],
            "wall_seconds": [5_468_714_802, 5_463_957_204, 5_461_918_456,
                             8_008_123_755, 8_007_549_453],
        }
    )
    profile["normalized_time"] = profile["wall_seconds"] / profile["wall_seconds"].max()
    delta["normalized_time"] = delta["wall_seconds"] / delta["wall_seconds"].max()

    plt.rcParams.update(
        {
            "font.family": "Times New Roman",
            "mathtext.fontset": "stix",
            "font.size": 12,
            "axes.labelsize": 13,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
            "legend.fontsize": 10,
        }
    )

    figure = plt.figure(figsize=(8.5, 3.0))
    grid = gridspec.GridSpec(1, 5, width_ratios=[0.85, 0.6, 0.85, 0.08, 0.85], wspace=0)
    machine_axis = figure.add_subplot(grid[0])
    profile_axis = figure.add_subplot(grid[2])
    delta_axis = figure.add_subplot(grid[4])

    # Panel (a): the calibrated estimate, explicitly annotated as modelled.
    machine_positions = np.arange(len(estimate))
    overhead = estimate["modeled_sync_overhead_percent"].to_numpy()
    total_time = estimate["modeled_total_simulation_seconds"].to_numpy()
    machines = estimate["nodes"].to_numpy()
    machine_axis.bar(
        machine_positions, overhead, width=0.45, color="#FAA36E", edgecolor="black",
        linewidth=1.0, label="Sync. Overhead", zorder=2,
    )
    machine_axis.set_xlabel("Number of Machines", fontweight="bold")
    machine_axis.set_ylabel("Sync. Overhead (%)", fontweight="bold")
    machine_axis.set_xticks(machine_positions, machines, fontweight="bold")
    machine_axis.set_ylim(0, 1.85)
    machine_axis.grid(axis="y", linestyle="--", linewidth=0.8, alpha=0.5)
    machine_axis.set_axisbelow(True)

    time_axis = machine_axis.twinx()
    time_axis.plot(
        machine_positions, total_time, color="#800026", marker="o", markersize=7,
        linewidth=2, label="Total Sim. Time", zorder=3,
    )
    time_axis.set_ylabel("Total Sim. Time (s)", fontweight="bold")
    time_axis.set_ylim(4.5, 10.5)

    for index, value in enumerate(overhead):
        machine_axis.annotate(
            f"{value:.2f}%", xy=(machine_positions[index], value), xytext=(0, 4),
            textcoords="offset points", ha="center", va="bottom", fontsize=9,
            color="black", fontweight="bold",
        )
    for index, value in enumerate(total_time):
        time_axis.annotate(
            f"{value:.2f}", xy=(machine_positions[index], value), xytext=(0, 6),
            textcoords="offset points", ha="center", va="bottom", fontsize=9,
            color="#E45756", fontweight="bold",
        )
    handles_left, _ = machine_axis.get_legend_handles_labels()
    handles_right, _ = time_axis.get_legend_handles_labels()
    machine_axis.legend(
        handles_left + handles_right, ["Overhead", "Total Time"], loc="upper center",
        bbox_to_anchor=(0.5, 1.30), ncol=2, frameon=False, columnspacing=0.8,
    )
    machine_axis.text(
        0.5, -0.35, "(a) Multi-machine (model estimate)", transform=machine_axis.transAxes,
        ha="center", va="top", fontweight="bold",
    )

    # Panel (b), left: profile-window sensitivity from the provided reference.
    profile_positions = np.arange(len(profile))
    profile_axis.bar(
        profile_positions, profile["normalized_time"], width=0.5, color="#FFE199",
        edgecolor="black", linewidth=1.0,
    )
    profile_axis.set_xlabel(r"$\Delta T_{\mathrm{prof}}$ (cycles)", fontweight="bold")
    profile_axis.set_ylabel("Normalized Sim. Time", fontweight="bold")
    profile_axis.set_xticks(profile_positions, [format_ticks(value) for value in profile["profile_window"]], fontweight="bold")
    profile_axis.grid(axis="y", linestyle="--", alpha=0.35)
    profile_axis.set_axisbelow(True)

    # Panel (b), right: phase-cycle sensitivity from the provided reference.
    delta_positions = np.arange(len(delta))
    delta_axis.bar(
        delta_positions, delta["normalized_time"], width=0.5, color="#FC757B",
        edgecolor="black", linewidth=1.0,
    )
    delta_axis.set_xlabel(r"$\delta$ (cycles)", fontweight="bold")
    delta_axis.set_xticks(delta_positions, [format_ticks(value) for value in delta["phase_cycles"]], fontweight="bold")
    delta_axis.tick_params(axis="y", labelleft=False)
    delta_axis.grid(axis="y", linestyle="--", alpha=0.35)
    delta_axis.set_axisbelow(True)

    maximum = math.ceil(max(profile["normalized_time"].max(), delta["normalized_time"].max()) / 0.2) * 0.2 + 0.2
    profile_axis.set_ylim(0, maximum)
    delta_axis.set_ylim(0, maximum)
    figure.text(0.705, -0.01, "(b) Runtime Parameters", ha="center", va="top", fontweight="bold")
    figure.subplots_adjust(left=0.08, right=0.92, bottom=0.25, top=0.78)

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(arguments.output, dpi=300, bbox_inches="tight")


if __name__ == "__main__":
    main()
