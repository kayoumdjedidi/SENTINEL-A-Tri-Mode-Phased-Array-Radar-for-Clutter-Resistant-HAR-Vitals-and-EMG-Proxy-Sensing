"""SENTINEL Stage 1 -- offline range-Doppler replay tool.

Loads a saved capture in either ADI format (a .npy + _config.npy pair) or a
SENTINEL session directory (produced by `sentinel_mode_a.py --save`), then
replays every frame through the shared DSP stack from `sentinel_core`.

The output is deterministic: given the same file and options, every run
produces range-Doppler maps with identical shape and values.

Usage
-----
Replay a SENTINEL session directory::

    python offline_replay.py recordings/20260403_124003

Replay a raw ADI-format capture::

    python offline_replay.py notdd_4MSPS_500M_500u_64.npy

Write processed frames to a directory instead of displaying them::

    python offline_replay.py recordings/20260403_124003 --no-plot --out processed/

Replay with MTI disabled and CFAR enabled::

    python offline_replay.py capture.npy --no-mti --cfar

The config schema (7-element array) must be compatible with
`Range_Doppler_Processing.py`:

    [sample_rate, signal_freq, output_freq, num_chirps, chirp_BW, ramp_time_s, frame_length_ms]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offline_replay",
        description="Offline range-Doppler replay from saved SENTINEL captures",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Session directory or .npy file path",
    )
    mti_group = parser.add_mutually_exclusive_group()
    mti_group.add_argument(
        "--mti", dest="mti", action="store_true", default=True,
        help="Apply 2-pulse MTI filter (default)",
    )
    mti_group.add_argument(
        "--no-mti", dest="mti", action="store_false",
        help="Disable MTI filter",
    )
    parser.add_argument(
        "--cfar", action="store_true", default=False,
        help="Enable CFAR detection on range profile",
    )
    parser.add_argument(
        "--cfar-guard", type=int, default=15, metavar="N",
        help="CFAR guard cells",
    )
    parser.add_argument(
        "--cfar-ref", type=int, default=16, metavar="N",
        help="CFAR reference cells",
    )
    parser.add_argument(
        "--cfar-bias", type=float, default=25.0,
        help="CFAR bias (dB)",
    )
    parser.add_argument(
        "--range-bin", type=int, default=None, metavar="BIN",
        help="Manual range gate bin (auto if omitted)",
    )
    parser.add_argument(
        "--min-range-bin", type=int, default=5, metavar="BIN",
        help="Minimum bin for auto range gate",
    )
    parser.add_argument(
        "--min-scale", type=float, default=0.0,
        help="Range-Doppler colormap minimum (log10 units)",
    )
    parser.add_argument(
        "--max-scale", type=float, default=8.0,
        help="Range-Doppler colormap maximum (log10 units)",
    )
    parser.add_argument(
        "--max-range", type=float, default=100.0, metavar="m",
        help="Display range axis upper limit",
    )
    parser.add_argument(
        "--frames", type=int, default=None, metavar="N",
        help="Maximum frames to replay (default: all)",
    )
    parser.add_argument(
        "--no-plot", dest="plot", action="store_false", default=True,
        help="Skip live display (useful with --out)",
    )
    parser.add_argument(
        "--out", type=Path, default=None, metavar="DIR",
        help="Write range-Doppler frames as .npy files to this directory",
    )
    parser.add_argument(
        "--pause", type=float, default=0.05, metavar="s",
        help="Pause between frames when plotting",
    )
    return parser


# ---------------------------------------------------------------------------
# Capture loader (handles both legacy and current session schemas)
# ---------------------------------------------------------------------------

def _load_capture(source: Path, load_adi_capture_fn):
    """Load a capture from a session directory or .npy file.

    Falls back to a permissive JSON parser that handles the older session
    schema (e.g. `chirp_BW` vs `chirp_bw`, absent `frame_length_ms`, etc.)
    if the standard loader raises a `KeyError`.
    """
    try:
        return load_adi_capture_fn(source)
    except (KeyError, Exception):
        pass

    # -- permissive fallback for legacy session directories ---------------
    import json
    from sentinel_core import RadarConfig, import_adi_config

    if source.is_dir():
        raw_bursts = np.load(source / "raw_bursts.npy")
        config_json = source / "config.json"
        if config_json.exists():
            payload = json.loads(config_json.read_text(encoding="utf-8"))
            sample_rate = float(payload.get("sample_rate", 4e6))
            signal_freq = float(payload.get("signal_freq", 100e3))
            output_freq = float(payload.get("output_freq", 10.25e9))
            num_chirps = int(payload.get("num_chirps", 64))
            # Accept both chirp_bw (new) and chirp_BW (legacy)
            chirp_bw = float(
                payload.get("chirp_bw", payload.get("chirp_BW", 500e6))
            )
            ramp_time_s = float(
                payload.get(
                    "ramp_time_s",
                    payload.get("ramp_time_us", 500) / 1e6,
                )
            )
            # frame_length_ms: new schema stores it directly; legacy can
            # derive it from ramp_time_us (continuous: no guard) or N_frame
            frame_length_ms = float(
                payload.get(
                    "frame_length_ms",
                    ramp_time_s * 1e3,
                )
            )
            # samples_per_chirp: prefer N_frame from legacy schema
            samples_per_chirp = int(
                payload.get("N_frame", raw_bursts.shape[-1])
            )
            config = RadarConfig(
                sample_rate=sample_rate,
                signal_freq=signal_freq,
                output_freq=output_freq,
                num_chirps=num_chirps,
                chirp_bw=chirp_bw,
                ramp_time_s=ramp_time_s,
                frame_length_ms=frame_length_ms,
                samples_per_chirp=samples_per_chirp,
            )
            return raw_bursts, config

        config_npy = source / "raw_bursts_config.npy"
        if config_npy.exists():
            config = import_adi_config(
                np.load(config_npy),
                samples_per_chirp=raw_bursts.shape[-1],
            )
            return raw_bursts, config

    raise RuntimeError(
        f"Cannot load capture from {source}: no recognised config found."
    )


# ---------------------------------------------------------------------------
# Core replay logic
# ---------------------------------------------------------------------------

def replay(args: argparse.Namespace) -> int:
    """Load, process, and optionally display saved radar frames.

    Returns
    -------
    int
        Exit code (0 = success).
    """
    # -- imports (no hardware required) -----------------------------------
    import sys
    _dir = Path(__file__).resolve().parent
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

    from sentinel_core import (  # type: ignore
        load_adi_capture,
        process_frame,
        range_axis,
        velocity_axis,
    )

    # -- load -------------------------------------------------------------
    source = args.source.expanduser().resolve()
    if not source.exists():
        print(f"[offline_replay] source not found: {source}", file=sys.stderr)
        return 1

    print(f"Loading: {source}")
    all_bursts, config = _load_capture(source, load_adi_capture)

    # all_bursts shape is either
    #   (n_frames, n_chirps, n_samples)  -- SENTINEL session
    #   (n_frames, n_chirps, n_samples)  -- ADI save_data format (same)
    # Ensure 3D
    all_bursts = np.asarray(all_bursts, dtype=np.complex64)
    if all_bursts.ndim == 2:
        # single frame stored as (n_chirps, n_samples)
        all_bursts = all_bursts[np.newaxis, ...]

    n_frames = all_bursts.shape[0]
    if args.frames is not None:
        n_frames = min(n_frames, args.frames)

    print(
        f"  frames={n_frames}  chirps={config.num_chirps}  "
        f"spc={config.effective_samples_per_chirp}  "
        f"bw={config.chirp_bw/1e6:.0f}MHz  "
        f"ramp={int(config.ramp_time_s*1e6)}us  "
        f"R_res={config.range_resolution_m:.3f}m  "
        f"v_res={config.velocity_resolution_m_s:.3f}m/s"
    )

    # -- output directory -------------------------------------------------
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)

    # -- plotting setup ---------------------------------------------------
    fig = ax = img = None
    plt = None
    if args.plot:
        try:
            import matplotlib
            import matplotlib.pyplot as _plt
            plt = _plt
            matplotlib.use("TkAgg" if "linux" in sys.platform else "Qt5Agg")

            dist = range_axis(config)
            vel = velocity_axis(config)
            extent = [vel.min(), vel.max(), dist.min(), dist.max()]

            fig, ax = plt.subplots(figsize=(10, 6))
            dummy = np.zeros(
                (config.effective_samples_per_chirp, config.num_chirps),
                dtype=np.float32,
            )
            try:
                cmap = matplotlib.colormaps.get_cmap("inferno")
            except AttributeError:
                from matplotlib.cm import get_cmap
                cmap = get_cmap("inferno")

            img = ax.imshow(
                dummy, aspect="auto", extent=extent, origin="lower",
                cmap=cmap, vmin=args.min_scale, vmax=args.max_scale,
            )
            ax.set_title("Range-Doppler Replay")
            ax.set_xlabel("Velocity [m/s]")
            ax.set_ylabel("Range [m]")
            ax.set_ylim([0, args.max_range])
            ax.set_xlim([vel.min(), vel.max()])
            plt.colorbar(img, ax=ax, label="log10(magnitude)")
            plt.tight_layout()
            plt.show(block=False)
        except ImportError:
            print("[offline_replay] matplotlib not available; running headless.")
            plt = None

    # -- replay loop ------------------------------------------------------
    processed_frames: list[np.ndarray] = []

    for frame_idx in range(n_frames):
        chirp_matrix = all_bursts[frame_idx]

        result = process_frame(
            chirp_matrix,
            apply_mti=args.mti,
            apply_cfar=args.cfar,
            manual_range_bin=args.range_bin,
            min_range_bin=args.min_range_bin,
            cfar_guard=args.cfar_guard,
            cfar_ref=args.cfar_ref,
            cfar_bias=args.cfar_bias,
            min_scale=args.min_scale,
            max_scale=args.max_scale,
        )
        rd = result["range_doppler"]
        processed_frames.append(rd)

        if frame_idx % 20 == 0 or frame_idx == n_frames - 1:
            print(
                f"  frame {frame_idx + 1}/{n_frames}  "
                f"range_bin={result['selected_range_bin']}  "
                f"rd_shape={rd.shape}"
            )

        # live plot update
        if plt is not None and img is not None:
            img.set_data(rd)
            fig.canvas.draw_idle()
            plt.pause(args.pause)

        # write frame to disk
        if args.out is not None:
            frame_path = args.out / f"frame_{frame_idx:05d}.npy"
            np.save(frame_path, rd)

    # -- summary ----------------------------------------------------------
    shapes = {f.shape for f in processed_frames}
    print(f"\nReplay complete: {len(processed_frames)} frames processed.")
    if len(shapes) == 1:
        print(f"  Output shape (consistent): {shapes.pop()}")
    else:
        print(f"  WARNING: inconsistent output shapes: {shapes}")

    if args.out is not None:
        print(f"  Frames written to: {args.out}")

    if plt is not None:
        print("  Close the plot window or press CTRL+C to exit.")
        try:
            plt.show(block=True)
        except KeyboardInterrupt:
            pass

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return replay(args)


if __name__ == "__main__":
    sys.exit(main())
