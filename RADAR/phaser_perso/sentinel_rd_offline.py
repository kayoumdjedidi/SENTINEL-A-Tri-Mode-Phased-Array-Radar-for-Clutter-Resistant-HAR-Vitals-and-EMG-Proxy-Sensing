#!/usr/bin/env python3
"""Offline range-Doppler replay for SENTINEL captures."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from sentinel_core import (
    load_adi_capture,
    process_frame,
    range_axis,
    velocity_axis,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay saved SENTINEL captures offline")
    parser.add_argument("capture", help="Path to raw capture .npy or a session directory")
    parser.add_argument("--frame", type=int, default=0, help="Starting frame index")
    parser.add_argument("--loop", action="store_true", help="Loop through all frames")
    parser.add_argument("--step-ms", type=int, default=100)
    parser.add_argument(
        "--plot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Plot with matplotlib when available",
    )
    parser.add_argument(
        "--mti",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--cfar",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--cfar-bias", type=float, default=25.0)
    parser.add_argument("--cfar-guard", type=int, default=15)
    parser.add_argument("--cfar-ref", type=int, default=16)
    parser.add_argument("--range-gate", type=int, default=None)
    parser.add_argument("--min-range-bin", type=int, default=1)
    return parser


def _setup_plot(config):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not available; printing summaries only.")
        return None

    fig, ax = plt.subplots(1, figsize=(8, 6))
    image = ax.imshow(
        np.zeros((config.effective_samples_per_chirp, config.num_chirps), dtype=np.float32),
        aspect="auto",
        origin="lower",
        extent=[
            velocity_axis(config).min(),
            velocity_axis(config).max(),
            range_axis(config).min(),
            range_axis(config).max(),
        ],
        cmap="inferno",
    )
    ax.set_title("Offline Range-Doppler Replay")
    ax.set_xlabel("Velocity [m/s]")
    ax.set_ylabel("Range [m]")
    fig.tight_layout()
    plt.show(block=False)
    return {"plt": plt, "image": image}


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    raw_bursts, config = load_adi_capture(Path(args.capture))
    if raw_bursts.ndim == 2:
        raw_bursts = raw_bursts[np.newaxis, ...]

    plot_state = _setup_plot(config) if args.plot else None
    frame_index = max(0, min(args.frame, raw_bursts.shape[0] - 1))

    try:
        while True:
            chirps = np.asarray(raw_bursts[frame_index], dtype=np.complex64)
            result = process_frame(
                chirps,
                apply_mti=args.mti,
                apply_cfar=args.cfar,
                manual_range_bin=args.range_gate,
                min_range_bin=args.min_range_bin,
                cfar_guard=args.cfar_guard,
                cfar_ref=args.cfar_ref,
                cfar_bias=args.cfar_bias,
            )

            print(
                f"frame={frame_index} selected_range_bin={result['selected_range_bin']} "
                f"peak={float(result['range_profile'][result['selected_range_bin']]):.3f}"
            )

            if plot_state is not None:
                plot_state["image"].set_data(result["range_doppler"])
                plot_state["plt"].pause(args.step_ms / 1000)

            if not args.loop:
                break

            frame_index = (frame_index + 1) % raw_bursts.shape[0]
    except KeyboardInterrupt:
        print("Interrupted by user.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
