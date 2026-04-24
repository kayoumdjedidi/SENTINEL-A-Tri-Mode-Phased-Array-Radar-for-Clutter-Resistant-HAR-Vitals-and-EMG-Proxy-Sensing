"""SENTINEL Mode A entry point.

Composes radar_dsp + radar_backends into a runnable session with live display.

Usage
-----
    python radar_mode_a.py --acq continuous
    python radar_mode_a.py --acq tdd --frames 100 --bw 500e6 --ramp-time-us 500
    python radar_mode_a.py --acq continuous --no-plot --no-save  # headless

Acquisition modes
-----------------
  continuous  -- free-running sawtooth, no TDD, safe over WSL->Pi->Pluto
                 (Range_Doppler_NoTDD.py reference path)
  tdd         -- single_sawtooth_burst with TDD engine sync
                 (reference/Range_Doppler_Plot.py reference path)
                 Requires Pluto firmware >= 0.39

Default tuning ladder (final_PLAN.md)
--------------------------------------
  --bw 500e6 --ramp-time-us 500 --num-chirps 64    <- default, start here
  --bw 500e6 --ramp-time-us 500 --num-chirps 128
  --bw 200e6 --ramp-time-us 500 --num-chirps 128
  --bw 200e6 --ramp-time-us 200 --num-chirps 128
  --bw 200e6 --ramp-time-us 200 --num-chirps 256   <- only after stability
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np


log = logging.getLogger("radar_mode_a")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the Mode A argument parser.

    Import-safe: no hardware or GUI imports so tests can use the parser
    without adi / matplotlib installed.
    """
    p = argparse.ArgumentParser(
        prog="radar_mode_a",
        description="SENTINEL Mode A: FMCW range-Doppler + micro-Doppler",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # -- acquisition --
    p.add_argument(
        "--acq",
        choices=["tdd", "continuous"],
        default="continuous",
        metavar="{tdd,continuous}",
        help="Acquisition backend",
    )
    p.add_argument(
        "--frames",
        type=int,
        default=None,
        metavar="N",
        help="Frames to collect before stopping; omit or use 0 to run until CTRL+C",
    )

    # -- radar tuning --
    t = p.add_argument_group("radar tuning")
    t.add_argument("--bw", type=float, default=500e6, metavar="Hz",
                   help="Chirp bandwidth")
    t.add_argument("--ramp-time-us", type=int, default=500, metavar="us",
                   help="Ramp time in microseconds")
    t.add_argument("--num-chirps", type=int, default=128,
                   help="Chirps per frame. 128 = 64 ms/frame (~15 FPS real-time feel. "
                        "Use 256 for recording/offline analysis (128 ms/frame, +3 dB SNR))")
    t.add_argument("--sample-rate", type=float, default=4e6, metavar="Hz",
                   help="ADC sample rate")
    t.add_argument("--rx-gain", type=int, default=70,
                   help="RX gain dB (-3 to 70)")
    t.add_argument("--tx-gain", type=int, default=0,
                   help="TX gain dB (0 to -88; 0 = max power)")
    t.add_argument("--output-freq", type=float, default=10.25e9, metavar="Hz",
                   help="RF output frequency")
    t.add_argument("--center-freq", type=float, default=2.1e9, metavar="Hz",
                   help="SDR LO frequency")
    t.add_argument("--signal-freq", type=float, default=100e3, metavar="Hz",
                   help="IF tone frequency")
    t.add_argument(
        "--gain-taper",
        default="127,127,127,127,127,127,127,127",
        metavar="g0,..,g7",
        help="8 ADAR1000 channel gains (0-127), comma-separated",
    )
    t.add_argument("--steer", type=float, default=0.0, metavar="DEG",
                   help="Beam steering angle in degrees")

    # -- DSP --
    d = p.add_argument_group("DSP")
    mti_grp = d.add_mutually_exclusive_group()
    mti_grp.add_argument("--mti", dest="mti", action="store_true", default=True,
                         help="Enable 2-pulse MTI filter (default on). "
                              "NOTE: MTI suppresses zero-velocity targets -- "
                              "use --no-mti when testing with a stationary reflector")
    mti_grp.add_argument("--no-mti", dest="mti", action="store_false",
                         help="Disable MTI (required to see stationary targets)")
    win_grp = d.add_mutually_exclusive_group()
    win_grp.add_argument("--window", dest="window", action="store_true", default=True,
                         help="2D Hanning window before FFT (default on; -32 dB sidelobes)")
    win_grp.add_argument("--no-window", dest="window", action="store_false",
                         help="Disable 2D windowing (-13 dB rectangular sidelobes)")
    gate_grp = d.add_mutually_exclusive_group()
    gate_grp.add_argument("--range-bin", type=int, default=None, metavar="BIN",
                          help="Fix range gate by FFT bin")
    gate_grp.add_argument("--range-m", type=float, default=None, metavar="M",
                          help="Fix range gate by physical distance in metres")
    min_gate_grp = d.add_mutually_exclusive_group()
    min_gate_grp.add_argument("--min-range-bin", type=int, default=None, metavar="BIN",
                              help="Auto-gate lower bound in FFT bins")
    min_gate_grp.add_argument("--min-range-m", type=float, default=3.0, metavar="M",
                              help="Auto-gate lower bound in metres")
    d.add_argument("--history-len", type=int, default=64,
                   help="Micro-Doppler history depth in frames")
    d.add_argument("--min-scale", type=float, default=0.0,
                   help="Range-Doppler colormap floor (log10)")
    d.add_argument("--max-scale", type=float, default=8.0,
                   help="Range-Doppler colormap ceiling (log10)")
    d.add_argument("--max-range", type=float, default=100.0, metavar="m",
                   help="Display range axis upper limit (metres)")
    d.add_argument("--max-vel", type=float, default=10.0, metavar="m/s",
                   help="Display velocity axis half-width (metres/s)")

    # -- display --
    disp = p.add_argument_group("display")
    disp.add_argument("--no-plot", dest="plot", action="store_false", default=True,
                      help="Disable live matplotlib display")

    # -- recording --
    r = p.add_argument_group("recording")
    r.add_argument("--save-dir", type=Path, default=Path("recordings"),
                   metavar="DIR", help="Base directory for session output")
    r.add_argument("--no-save", action="store_true",
                   help="Disable all recording output")
    r.add_argument("--session-name", default=None,
                   help="Session name override (default: timestamp)")

    # -- hardware URIs --
    hw = p.add_argument_group("hardware")
    hw.add_argument("--rpi-uri", default="ip:phaser.local",
                    help="Phaser IIO URI")
    hw.add_argument("--sdr-uri", default="ip:phaser.local:50901",
                    help="Pluto SDR IIO URI (context forwarding)")
    hw.add_argument(
        "--tdd-uri",
        default=None,
        help="Pluto GPIO/TDD IIO URI for --acq tdd (default: same as --sdr-uri)",
    )

    # -- TDD-only --
    tdd = p.add_argument_group("TDD options (--acq tdd only)")
    tdd.add_argument("--frame-guard-ms", type=float, default=0.2,
                     help="Guard interval added to ramp to form PRI")
    tdd.add_argument("--no-tdd-ext-sync", dest="tdd_ext_sync",
                     action="store_false", default=True,
                     help="Use internal TDD trigger instead of external sync")
    tdd.add_argument("--tdd-arm-delay-s", type=float, default=0.5,
                     help="Seconds to wait after starting rx() before pulsing gpio_burst")

    return p


# ---------------------------------------------------------------------------
# Live display (matplotlib)
# ---------------------------------------------------------------------------

class _LiveDisplay:
    """Two-panel live display: Range-Doppler map + Micro-Doppler history.

    Layout mirrors reference/Range_Doppler_Plot.py but adds the rolling
    micro-Doppler history panel on the right.
    """

    def __init__(self, cfg, args) -> None:
        import matplotlib
        # TkAgg is the fastest interactive backend on Linux.
        # Must be called before pyplot import; ignore if already set.
        try:
            matplotlib.use("TkAgg")
        except Exception:
            pass
        import matplotlib.pyplot as plt

        self._plt = plt
        dist = cfg.range_axis()
        vel = cfg.velocity_axis()

        fig, (ax_rd, ax_md) = plt.subplots(
            1, 2, figsize=(16, 7),
            gridspec_kw={"width_ratios": [2, 1]},
        )
        fig.suptitle(
            f"SENTINEL Mode A  [{args.acq}]  "
            f"BW={cfg.chirp_bw/1e6:.0f}MHz  "
            f"ramp={int(cfg.ramp_time_s*1e6)}us  "
            f"chirps={cfg.num_chirps}  "
            f"MTI={'on' if args.mti else 'off'}  "
            f"win={'on' if args.window else 'off'}",
            fontsize=12,
        )

        # -- Range-Doppler panel --
        n_range = cfg.n_frame
        n_doppler = cfg.num_chirps
        dummy_rd = np.zeros((n_range, n_doppler), dtype=np.float32)
        rd_extent = [vel.min(), vel.max(), dist.min(), dist.max()]

        try:
            cmap_rd = matplotlib.colormaps.get_cmap("inferno")
        except AttributeError:
            from matplotlib.cm import get_cmap
            cmap_rd = get_cmap("inferno")

        # No vmin/vmax: let matplotlib auto-scale on the first frame.
        # Per-frame set_clim() in update() then locks dynamic contrast on
        # every frame, matching the behaviour of the reference scripts.
        self._img_rd = ax_rd.imshow(
            dummy_rd, aspect="auto", extent=rd_extent, origin="lower",
            cmap=cmap_rd,
        )
        ax_rd.set_title("Range-Doppler Spectrum", fontsize=14)
        ax_rd.set_xlabel("Velocity [m/s]", fontsize=12)
        ax_rd.set_ylabel("Range [m]", fontsize=12)
        ax_rd.set_xlim([-args.max_vel, args.max_vel])
        ax_rd.set_ylim([0, args.max_range])
        ax_rd.set_yticks(np.arange(0, args.max_range, args.max_range / 10))
        fig.colorbar(self._img_rd, ax=ax_rd, label="log10 magnitude")

        # -- Micro-Doppler history panel --
        history_len = args.history_len
        dummy_md = np.zeros((n_doppler, history_len), dtype=np.float32)
        md_extent = [0, history_len, vel.min(), vel.max()]

        try:
            cmap_md = matplotlib.colormaps.get_cmap("viridis")
        except AttributeError:
            from matplotlib.cm import get_cmap
            cmap_md = get_cmap("viridis")

        self._img_md = ax_md.imshow(
            dummy_md, aspect="auto", extent=md_extent, origin="lower",
            cmap=cmap_md,
        )
        ax_md.set_title("Micro-Doppler History", fontsize=14)
        ax_md.set_xlabel("Frame index", fontsize=12)
        ax_md.set_ylabel("Velocity [m/s]", fontsize=12)
        ax_md.set_xlim([0, history_len])
        ax_md.set_ylim([-args.max_vel, args.max_vel])

        self._ax_rd = ax_rd
        self._range_bin_line = ax_rd.axhline(
            y=0, color="cyan", linewidth=1, linestyle="--", alpha=0.6
        )
        self._frame_label = fig.text(
            0.5, 0.01, "frame 0", ha="center", fontsize=10, color="gray"
        )

        plt.tight_layout(rect=[0, 0.03, 1, 1])
        plt.show(block=False)
        plt.pause(0.05)
        self._fig = fig
        self._cfg = cfg
        self._dist = dist

    def update(self, result: dict, history_data: np.ndarray, frame_idx: int) -> None:
        rd = result["rd_map"]
        self._img_rd.set_data(rd)
        # Per-frame dynamic normalization: 2nd–99.5th percentile.
        # This adapts to the actual signal level each frame and gives the same
        # high-contrast look as the reference scripts (which also auto-scale).
        lo, hi = np.percentile(rd, [2.0, 99.5])
        if hi > lo:
            self._img_rd.set_clim(lo, hi)

        # show the selected range gate as a horizontal line
        range_m = (
            float(result["range_m"])
            if result.get("range_m") is not None
            else float(self._dist[result["range_bin"]])
        )
        self._range_bin_line.set_ydata([range_m, range_m])
        # micro-Doppler history: shape (history_len, doppler_bins) -> display as .T
        md = history_data.T
        self._img_md.set_data(md)
        md_lo, md_hi = np.percentile(md, [2.0, 99.5])
        if md_hi > md_lo:
            self._img_md.set_clim(md_lo, md_hi)
        self._frame_label.set_text(
            f"frame {frame_idx}  |  range gate: {range_m:.1f} m  "
            f"(bin {result['range_bin']})"
        )
        # draw() + flush_events() is ~10x faster than plt.pause() because it
        # does not force a sleep in the Tcl/Tk event loop.  This matters here
        # because the frame rate is already bottlenecked by the SDR acquisition
        # time and we do not want the display to add extra latency on top.
        self._fig.canvas.draw()
        self._fig.canvas.flush_events()

    def close(self) -> None:
        try:
            self._plt.close(self._fig)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class ModeASession:
    """One complete Mode A acquisition run."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._backend = None
        self._cfg = None
        self._history = None
        self._display: _LiveDisplay | None = None
        self._raw_bursts: list[np.ndarray] = []
        self._spectrograms: list[np.ndarray] = []

    def connect(self) -> None:
        from radar_backends import HardwareParams, make_backend

        taper = tuple(
            int(v.strip()) for v in self.args.gain_taper.split(",") if v.strip()
        )
        if len(taper) != 8:
            raise ValueError("--gain-taper must supply exactly 8 values")

        hw = HardwareParams(
            rpi_uri=self.args.rpi_uri,
            sdr_uri=self.args.sdr_uri,
            tdd_uri=self.args.tdd_uri,
            sample_rate=self.args.sample_rate,
            center_freq=self.args.center_freq,
            signal_freq=self.args.signal_freq,
            rx_gain=self.args.rx_gain,
            tx_gain=self.args.tx_gain,
            output_freq=self.args.output_freq,
            chirp_bw=self.args.bw,
            ramp_time_us=self.args.ramp_time_us,
            num_chirps=self.args.num_chirps,
            gain_taper=taper,
            frame_guard_ms=self.args.frame_guard_ms,
            tdd_sync_external=self.args.tdd_ext_sync,
            tdd_ext_capture=self.args.tdd_ext_sync,
            tdd_arm_delay_s=self.args.tdd_arm_delay_s,
        )

        self._backend = make_backend(self.args.acq, hw)
        self._cfg = self._backend.connect()

        if self.args.steer != 0.0:
            self._backend.set_steering_angle(self.args.steer)

        print(
            f"[{self.args.acq}]  "
            f"rf={self._cfg.output_freq/1e9:.4f}GHz  "
            f"bw={self._cfg.chirp_bw/1e6:.0f}MHz  "
            f"ramp={int(self._cfg.ramp_time_s*1e6)}us  "
            f"chirps={self._cfg.num_chirps}  "
            f"spc={self._cfg.n_frame}  "
            f"R_res={self._cfg.range_res_m:.3f}m  "
            f"v_res={self._cfg.vel_res_m_s:.3f}m/s  "
            f"max_vel=±{self._cfg.max_vel_m_s:.1f}m/s"
        )

    def run(self) -> None:
        from radar_dsp import MicroDopplerHistory, process_frame

        if self._backend is None:
            raise RuntimeError("Call connect() before run()")

        self._history = MicroDopplerHistory(
            history_len=self.args.history_len,
            doppler_bins=self._cfg.num_chirps,
        )

        if self.args.plot:
            try:
                self._display = _LiveDisplay(self._cfg, self.args)
                print("Display open. Close the window or press CTRL+C to stop.")
            except Exception as exc:
                print(f"[display] Could not open GUI ({exc}); running headless.")
                self._display = None

        n_target = self.args.frames
        frame_idx = 0
        print("Acquiring... CTRL+C to stop.")
        range_axis_m = self._cfg.range_axis()

        try:
            while n_target is None or frame_idx < n_target:
                cap = self._backend.capture()
                result = process_frame(
                    cap.chirp_matrix,
                    apply_mti_filter=self.args.mti,
                    window=self.args.window,
                    manual_range_bin=self.args.range_bin,
                    manual_range_m=self.args.range_m,
                    min_range_bin=self.args.min_range_bin,
                    min_range_m=self.args.min_range_m,
                    range_axis_m=range_axis_m,
                    min_scale=self.args.min_scale,
                    max_scale=self.args.max_scale,
                )
                history_data = self._history.push(result["micro_doppler"])

                if not self.args.no_save:
                    self._raw_bursts.append(cap.chirp_matrix.copy())
                    self._spectrograms.append(result["micro_doppler"].copy())

                if self._display is not None:
                    self._display.update(result, history_data, frame_idx)

                frame_idx += 1
                if frame_idx % 20 == 0:
                    valid_n = (
                        int(np.sum(cap.valid))
                        if cap.valid is not None
                        else self._cfg.num_chirps
                    )
                    print(
                        f"  frame {frame_idx:4d}  "
                        f"range={result['range_m']:.1f}m  "
                        f"range_bin={result['range_bin']:4d}  "
                        f"valid_chirps={valid_n}/{self._cfg.num_chirps}"
                    )

        except KeyboardInterrupt:
            print(f"\nStopped after {frame_idx} frames.")

    def close(self) -> Path | None:
        if self._display is not None:
            self._display.close()

        if self._backend is not None:
            self._backend.close()

        if self.args.no_save or not self._raw_bursts:
            return None

        from radar_dsp import save_session

        sdir = save_session(
            out_dir=self.args.save_dir,
            cfg=self._cfg,
            raw_bursts=np.stack(self._raw_bursts, axis=0),
            spectrograms=np.stack(self._spectrograms, axis=0),
            session_name=self.args.session_name,
            labels={"segments": [], "acq_mode": self.args.acq},
            metadata={
                "acq": self.args.acq,
                "mti": self.args.mti,
                "steer_deg": self.args.steer,
            },
        )
        return sdir


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING,   # suppress INFO noise; use print() for user output
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    if args.frames is not None and args.frames <= 0:
        args.frames = None
    session = ModeASession(args)
    try:
        session.connect()
        session.run()
    finally:
        sdir = session.close()
        if sdir:
            print(f"\nRecording saved: {sdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
