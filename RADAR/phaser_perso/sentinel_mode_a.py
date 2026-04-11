#!/usr/bin/env python3
"""Reference-first SENTINEL Mode A runner.

This script keeps acquisition backends isolated from the shared DSP stack:
  - `--acq tdd` stays close to the golden `Range_Doppler_Plot.py` path
  - `--acq continuous` uses the proven no-TDD fallback

The module is intentionally import-safe so tests can exercise the CLI/parser
without requiring `adi`, `PyQt5`, or plotting libraries.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np

from sentinel_backends import (
    MissingRuntimeDependency,
    RuntimeRadarConfig,
    create_backend,
)
from sentinel_core import (
    MicroDopplerHistory,
    process_frame,
    range_axis,
    save_recording_session,
    velocity_axis,
)

TDD_INTERNAL_REFERENCE = {
    "ramp_time_us": 300,
    "num_chirps": 64,
    "tdd_sync_external": False,
    "tdd_ext_capture": False,
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SENTINEL Mode A FMCW runtime")
    parser.add_argument("--acq", choices=("tdd", "continuous"), default="tdd")
    parser.add_argument("--frames", type=int, default=0, help="0 means run until Ctrl+C")
    parser.add_argument(
        "--plot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable live matplotlib plots when available",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save raw bursts, spectrogram rows, and config on exit",
    )
    parser.add_argument("--session-name", default=None)
    parser.add_argument(
        "--recordings-dir",
        default=str(Path(__file__).resolve().parent / "recordings"),
    )
    parser.add_argument(
        "--mti",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply 2-pulse canceller before the 2D FFT",
    )
    parser.add_argument(
        "--cfar",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Compute CFAR thresholds for the range profile",
    )
    parser.add_argument("--cfar-bias", type=float, default=25.0)
    parser.add_argument("--cfar-guard", type=int, default=15)
    parser.add_argument("--cfar-ref", type=int, default=16)
    parser.add_argument("--range-gate", type=int, default=None)
    parser.add_argument("--min-range-bin", type=int, default=1)
    parser.add_argument("--history-length", type=int, default=200)
    parser.add_argument("--steer-angle", type=float, default=0.0)

    parser.add_argument("--sample-rate", type=float, default=4e6)
    parser.add_argument("--center-freq", type=float, default=2.1e9)
    parser.add_argument("--signal-freq", type=float, default=100e3)
    parser.add_argument("--rx-gain", type=int, default=60)
    parser.add_argument("--tx-gain", type=int, default=0)
    parser.add_argument("--output-freq", type=float, default=10.25e9)
    parser.add_argument("--chirp-bw", type=float, default=500e6)
    parser.add_argument("--ramp-time-us", type=int, default=500)
    parser.add_argument("--num-chirps", type=int, default=64)
    parser.add_argument("--frame-guard-ms", type=float, default=0.2)
    parser.add_argument(
        "--gain-taper",
        default="127,127,127,127,127,127,127,127",
        help="Comma-separated ADAR1000 gain taper",
    )
    parser.add_argument(
        "--tdd-sync-external",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--tdd-ext-capture",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--tdd-profile",
        choices=("auto", "custom", "internal_ref"),
        default="auto",
        help=(
            "TDD hardware preset. 'auto' uses the reduced-buffer internal-sync "
            "reference profile on the current WSL->Pi->Pluto path."
        ),
    )
    return parser


def build_runtime_config(args: argparse.Namespace) -> RuntimeRadarConfig:
    taper = tuple(int(value.strip()) for value in args.gain_taper.split(",") if value.strip())
    if len(taper) != 8:
        raise ValueError("gain_taper must provide exactly 8 integer values")

    runtime = RuntimeRadarConfig(
        sample_rate=args.sample_rate,
        center_freq=args.center_freq,
        signal_freq=args.signal_freq,
        rx_gain=args.rx_gain,
        tx_gain=args.tx_gain,
        output_freq=args.output_freq,
        chirp_bw=args.chirp_bw,
        ramp_time_us=args.ramp_time_us,
        num_chirps=args.num_chirps,
        gain_taper=taper,
        frame_guard_ms=args.frame_guard_ms,
        tdd_sync_external=args.tdd_sync_external,
        tdd_ext_capture=args.tdd_ext_capture,
    )
    if args.acq == "tdd" and args.tdd_profile in {"auto", "internal_ref"}:
        runtime.ramp_time_us = TDD_INTERNAL_REFERENCE["ramp_time_us"]
        runtime.num_chirps = TDD_INTERNAL_REFERENCE["num_chirps"]
        runtime.tdd_sync_external = TDD_INTERNAL_REFERENCE["tdd_sync_external"]
        runtime.tdd_ext_capture = TDD_INTERNAL_REFERENCE["tdd_ext_capture"]
    return runtime


def _setup_plots(config, history_length: int):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not available; running without live plots.")
        return None

    fig, (ax_rd, ax_md) = plt.subplots(1, 2, figsize=(14, 6))
    rd_extent = [
        velocity_axis(config).min(),
        velocity_axis(config).max(),
        range_axis(config).min(),
        range_axis(config).max(),
    ]
    md_extent = [
        0,
        history_length,
        velocity_axis(config).min(),
        velocity_axis(config).max(),
    ]
    rd_image = ax_rd.imshow(
        np.zeros((config.effective_samples_per_chirp, config.num_chirps), dtype=np.float32),
        aspect="auto",
        extent=rd_extent,
        origin="lower",
        cmap="inferno",
    )
    md_image = ax_md.imshow(
        np.zeros((config.num_chirps, history_length), dtype=np.float32),
        aspect="auto",
        extent=md_extent,
        origin="lower",
        cmap="viridis",
    )
    ax_rd.set_title("Range-Doppler")
    ax_rd.set_xlabel("Velocity [m/s]")
    ax_rd.set_ylabel("Range [m]")
    ax_md.set_title("Micro-Doppler History")
    ax_md.set_xlabel("Frame index")
    ax_md.set_ylabel("Velocity [m/s]")
    fig.tight_layout()
    plt.show(block=False)
    return {"plt": plt, "fig": fig, "rd_image": rd_image, "md_image": md_image}


def _update_plots(plot_state, config, result, history) -> None:
    plot_state["rd_image"].set_data(result["range_doppler"])
    plot_state["md_image"].set_data(history.data.T)
    plot_state["fig"].canvas.draw_idle()
    plot_state["plt"].pause(0.001)


def run_mode_a(args: argparse.Namespace) -> int:
    runtime = build_runtime_config(args)
    backend = create_backend(args.acq, runtime)
    plot_state = None
    history = None
    final_config = None

    raw_bursts_log: list[np.ndarray] = []
    raw_ch0_log: list[np.ndarray] = []
    raw_ch1_log: list[np.ndarray] = []
    valid_chirps_log: list[np.ndarray] = []
    spectrogram_rows: list[np.ndarray] = []

    try:
        final_config = backend.connect()
        print(
            f"[{args.acq}] profile="
            f"{args.tdd_profile if args.acq == 'tdd' else 'n/a'} "
            f"sample_rate={runtime.sample_rate/1e6:.1f}MHz "
            f"ramp={runtime.ramp_time_us}us "
            f"num_chirps={runtime.num_chirps} "
            f"frame_length_ms={final_config.frame_length_ms:.3f}"
        )
        if args.acq == "tdd":
            print(
                f"[tdd] sync_external={runtime.tdd_sync_external} "
                f"ext_capture={runtime.tdd_ext_capture} "
                f"rx_buffer_size={int(backend.my_sdr.rx_buffer_size)}"
            )
        backend.set_steering_angle(args.steer_angle)
        if args.plot:
            plot_state = _setup_plots(final_config, args.history_length)

        frame_idx = 0
        while args.frames == 0 or frame_idx < args.frames:
            capture = backend.capture_chirp_matrix()
            final_config = capture.config
            result = process_frame(
                capture.chirp_matrix,
                apply_mti=args.mti,
                apply_cfar=args.cfar,
                manual_range_bin=args.range_gate,
                min_range_bin=args.min_range_bin,
                cfar_guard=args.cfar_guard,
                cfar_ref=args.cfar_ref,
                cfar_bias=args.cfar_bias,
            )

            if history is None:
                history = MicroDopplerHistory(
                    history_length=args.history_length,
                    doppler_bins=result["micro_doppler"].size,
                )
            history.append(result["micro_doppler"])

            if args.save:
                raw_bursts_log.append(capture.chirp_matrix)
                spectrogram_rows.append(result["micro_doppler"])
                if capture.raw_ch0 is not None:
                    raw_ch0_log.append(capture.raw_ch0)
                if capture.raw_ch1 is not None:
                    raw_ch1_log.append(capture.raw_ch1)
                if capture.valid_chirps is not None:
                    valid_chirps_log.append(capture.valid_chirps)

            if plot_state is not None and history is not None:
                _update_plots(plot_state, final_config, result, history)

            if frame_idx == 0 or (frame_idx + 1) % 10 == 0:
                print(
                    f"[{args.acq}] frame={frame_idx + 1} "
                    f"selected_range_bin={result['selected_range_bin']} "
                    f"valid_chirps="
                    f"{int(np.sum(capture.valid_chirps)) if capture.valid_chirps is not None else capture.chirp_matrix.shape[0]}"
                )
            frame_idx += 1

    except KeyboardInterrupt:
        print("Interrupted by user.")
    except MissingRuntimeDependency as exc:
        print(exc)
        return 2
    finally:
        backend.close()

    if args.save and final_config is not None and raw_bursts_log:
        metadata: dict[str, Any] = {
            "acquisition": args.acq,
            "mti": args.mti,
            "cfar": args.cfar,
            "steer_angle_deg": args.steer_angle,
        }
        session_dir = save_recording_session(
            base_dir=args.recordings_dir,
            session_name=args.session_name,
            config=final_config,
            raw_bursts=np.asarray(raw_bursts_log, dtype=np.complex64),
            spectrogram_rows=np.asarray(spectrogram_rows, dtype=np.float32),
            metadata=metadata,
            raw_ch0=np.asarray(raw_ch0_log, dtype=np.complex64) if raw_ch0_log else None,
            raw_ch1=np.asarray(raw_ch1_log, dtype=np.complex64) if raw_ch1_log else None,
            valid_chirps=np.asarray(valid_chirps_log, dtype=bool) if valid_chirps_log else None,
        )
        print(f"Saved session to {session_dir}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_mode_a(args)


if __name__ == "__main__":
    raise SystemExit(main())
