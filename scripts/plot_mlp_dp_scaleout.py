#!/usr/bin/env python3
"""Plot MLP-DP DinD scale-out wall time and cross-node sync overhead.

The input CSV is the summary emitted by ``summarize_dind_mlp_dp.py``.  The
total simulation time is normalized against the one-machine row, while the
cross-node synchronization metric remains a percentage of the same wall time.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager


CHINESE_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path, help="Scale-out summary.csv")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output image path (for example, scaleout.png)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = pd.read_csv(args.input_csv).sort_values("nodes")
    required_columns = {
        "nodes",
        "total_simulation_seconds",
        "cross_node_sync_overhead_percent",
    }
    missing = required_columns.difference(data.columns)
    if missing:
        raise ValueError(f"Missing required CSV columns: {sorted(missing)}")
    if data.empty or data.iloc[0]["nodes"] != 1:
        raise ValueError("The CSV must contain the one-machine baseline row.")

    baseline_seconds = float(data.iloc[0]["total_simulation_seconds"])
    normalized_time = data["total_simulation_seconds"] / baseline_seconds
    machines = data["nodes"].to_numpy()
    sync_overhead = data["cross_node_sync_overhead_percent"].to_numpy()
    positions = np.arange(len(data))

    chinese_font_path = next(
        (font_path for font_path in CHINESE_FONT_CANDIDATES if font_path.is_file()),
        None,
    )
    if chinese_font_path is None:
        raise FileNotFoundError(
            "No supported Chinese font found. Checked: "
            + ", ".join(str(font_path) for font_path in CHINESE_FONT_CANDIDATES)
        )
    font_manager.fontManager.addfont(str(chinese_font_path))
    chinese_font_name = font_manager.FontProperties(fname=chinese_font_path).get_name()

    # Match the compact paper-figure styling used by sensitivity.py.
    plt.rcParams.update(
        {
            "font.family": [chinese_font_name, "DejaVu Sans"],
            "mathtext.fontset": "stix",
            "font.size": 12,
            "axes.labelsize": 13,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
            "legend.fontsize": 10,
        }
    )

    fig, left_axis = plt.subplots(figsize=(6.4, 4.0))
    right_axis = left_axis.twinx()
    # Draw the normalized-time series (and its labels) above the bars without
    # hiding the bar fill or the right-axis ticks.
    left_axis.set_zorder(right_axis.get_zorder() + 1)
    left_axis.patch.set_visible(False)

    bars = right_axis.bar(
        positions,
        sync_overhead,
        width=0.48,
        color="#FAA36E",
        edgecolor="black",
        linewidth=1.0,
        label="同步开销",
        zorder=2,
    )
    line = left_axis.plot(
        positions,
        normalized_time,
        color="#800026",
        marker="o",
        markersize=7,
        linewidth=2,
        label="归一化总仿真时长",
        zorder=3,
    )[0]

    left_axis.set_xlabel("机器数", fontweight="bold")
    left_axis.set_ylabel("归一化总仿真时长（单机 = 1）", fontweight="bold")
    right_axis.set_ylabel("同步开销（%）", fontweight="bold")
    left_axis.set_xticks(positions)
    left_axis.set_xticklabels(machines, fontweight="bold")
    left_axis.set_ylim(0.4, max(1.10, normalized_time.max() + 0.12))
    right_axis.set_ylim(0, max(60, sync_overhead.max() + 8))
    left_axis.grid(axis="y", linestyle="--", linewidth=0.8, alpha=0.5)
    left_axis.set_axisbelow(True)

    for label in (
        left_axis.get_xticklabels()
        + left_axis.get_yticklabels()
        + right_axis.get_yticklabels()
    ):
        label.set_fontweight("bold")

    for bar, value in zip(bars, sync_overhead):
        right_axis.annotate(
            f"{value:.1f}%",
            xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            fontweight="bold",
        )
    line_label_positions = {
        # Keep each value vertically aligned with its point.
        1: {"xytext": (0, -14), "ha": "center", "va": "top"},
        2: {"xytext": (0, -14), "ha": "center", "va": "top"},
        4: {"xytext": (0, 8), "ha": "center", "va": "bottom"},
        8: {"xytext": (0, -14), "ha": "center", "va": "top"},
    }
    for position, machine_count, value in zip(positions, machines, normalized_time):
        label_position = line_label_positions[int(machine_count)]
        left_axis.annotate(
            f"{value:.2f}",
            xy=(position, value),
            xytext=label_position["xytext"],
            textcoords="offset points",
            ha=label_position["ha"],
            va=label_position["va"],
            fontsize=9,
            color="#800026",
            fontweight="bold",
        )

    left_axis.legend(
        [line, bars],
        [line.get_label(), bars.get_label()],
        loc="upper center",
        bbox_to_anchor=(0.5, 1.25),
        ncol=2,
        frameon=False,
        columnspacing=0.9,
    )
    fig.subplots_adjust(left=0.15, right=0.85, bottom=0.18, top=0.78)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=300, bbox_inches="tight")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
