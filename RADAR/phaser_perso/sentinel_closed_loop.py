#!/usr/bin/env python3
"""SENTINEL closed-loop supervisor.

This is the integration path after the standalone modes became stable:

  * Mode A is always the supervisor: TDD soft-sync FMCW detects, gates, and
    tracks a person using the same backend/DSP/display conventions as
    sentinel_mode_a_v5.py.
  * Modes B and C are not run by a blind periodic timer.  Manual lock is the
    default safe bring-up path; auto-interrogation can be enabled explicitly.
  * A terminal calibration pass runs before the GUI:
      1. optional cached HB100 external-source frequency sweep,
      2. empty-scene clutter / clipping check,
      3. range-bounded coarse beam sweep with a person in the scene,
      4. initial range-gate and display-level estimates.
"""
from __future__ import annotations

import argparse
import collections
import gc
import json
import pickle
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from cw_dsp import CWProcessor, VitalsResult
from myo_dsp import MyoProcessor, MyoResult
from radar_backends import CaptureResult, HardwareParams, load_phaser_gain_phase_cal, make_backend
from radar_dsp import MicroDopplerHistory, process_frame
from target_detection_dbfs import cfar as _cfar


pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
pg.setConfigOption("background", "#1a1a2e")
pg.setConfigOption("foreground", "#dddddd")

CHEBYSHEV_TAPER = (8, 34, 84, 127, 127, 84, 34, 8)
SPEED_OF_LIGHT = 299_792_458.0
LABEL_KW = {"color": "#ccc", "font-size": "10pt"}


def _lut(name: str) -> np.ndarray:
    try:
        cmap = pg.colormap.get(name, source="matplotlib")
    except Exception:
        cmap = pg.colormap.get(name)
    return cmap.getLookupTable(0.0, 1.0, 256)


INFERNO_LUT = _lut("inferno")
VIRIDIS_LUT = _lut("viridis")


@dataclass
class CalibrationResult:
    created_at: str
    steer_deg: float
    target_range_m: float | None
    target_score: float
    rx_gain_db: int
    clip_fraction: float
    adc_peak: float
    rd_floor: float
    rd_ceil: float
    notes: list[str]
    path: str | None = None
    hb100_freq_hz: float | None = None
    hb100_cal_path: str | None = None

    @staticmethod
    def default(args: argparse.Namespace) -> "CalibrationResult":
        return CalibrationResult(
            created_at=datetime.now().isoformat(timespec="seconds"),
            steer_deg=float(args.steer),
            target_range_m=None,
            target_score=0.0,
            rx_gain_db=int(args.rx_gain),
            clip_fraction=0.0,
            adc_peak=0.0,
            rd_floor=2.0,
            rd_ceil=8.0,
            notes=["Calibration skipped."],
            hb100_freq_hz=None,
            hb100_cal_path=str(args.hb100_cal_file),
        )


@dataclass
class ModeAResult:
    rd_pos: np.ndarray
    profile: np.ndarray
    beat_fft_pos: np.ndarray | None
    cfar_threshold: np.ndarray | None
    range_m: float
    range_bin: int
    target_score: float
    micro_doppler: np.ndarray
    candidates: list[tuple[float, float]]


@dataclass
class PersonSignature:
    timestamp: str
    range_m: float
    resp_bpm: float
    resp_conf: float
    hr_bpm: float
    hr_conf: float
    myo_index: float | None
    myo_active: bool | None


class TargetTracker:
    """Single-target lock logic.

    This is deliberately simple: the final multi-person identity layer should
    sit above this, but the first closed-loop system needs one stable lock.
    """

    def __init__(self, min_frames: int, max_std_m: float, min_score: float) -> None:
        self.min_frames = int(min_frames)
        self.max_std_m = float(max_std_m)
        self.min_score = float(min_score)
        self._ranges = collections.deque(maxlen=max(4, self.min_frames))
        self._scores = collections.deque(maxlen=max(4, self.min_frames))
        self.locked = False
        self.range_m: float | None = None
        self.quality = 0.0
        self.last_lock_time = 0.0

    def update(self, range_m: float, score: float) -> bool:
        valid = np.isfinite(range_m) and score >= self.min_score and range_m > 0.0
        if valid:
            self._ranges.append(float(range_m))
            self._scores.append(float(score))
        else:
            self._ranges.clear()
            self._scores.clear()
            self.locked = False
            self.range_m = None
            self.quality = 0.0
            return False

        if len(self._ranges) < self.min_frames:
            self.locked = False
            self.quality = len(self._ranges) / max(1, self.min_frames)
            return False

        r = np.array(self._ranges, dtype=float)
        s = np.array(self._scores, dtype=float)
        stable = float(np.std(r)) <= self.max_std_m
        self.locked = bool(stable)
        self.range_m = float(np.median(r))
        score_term = float(np.clip((np.mean(s) - self.min_score) / max(self.min_score, 1e-6), 0.0, 1.0))
        std_term = float(np.clip(1.0 - np.std(r) / max(self.max_std_m, 1e-6), 0.0, 1.0))
        self.quality = 0.5 * score_term + 0.5 * std_term
        if self.locked:
            self.last_lock_time = time.monotonic()
        return self.locked

    def reset(self) -> None:
        self._ranges.clear()
        self._scores.clear()
        self.locked = False
        self.range_m = None
        self.quality = 0.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel_closed_loop",
        description="SENTINEL closed-loop target-driven supervisor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    hw = p.add_argument_group("hardware URIs")
    hw.add_argument("--rpi-uri", default="ip:phaser.local")
    hw.add_argument("--sdr-uri", default="ip:192.168.2.1")
    hw.add_argument("--tdd-uri", default=None)

    a = p.add_argument_group("Mode A FMCW supervisor")
    a.add_argument("--acq", choices=["tdd", "continuous"], default="tdd")
    a.add_argument("--tdd-trigger", choices=["soft", "internal", "external"], default="soft")
    a.add_argument("--sample-rate", type=float, default=4e6)
    a.add_argument("--bw", type=float, default=500e6)
    a.add_argument("--ramp-time-us", type=int, default=300)
    a.add_argument(
        "--num-chirps",
        type=int,
        default=64,
        help="Closed-loop default is kept responsive. Use 128/256 for high-resolution Mode A-only tuning.",
    )
    a.add_argument("--output-freq", type=float, default=10.25e9)
    a.add_argument("--center-freq", type=float, default=2.1e9)
    a.add_argument("--signal-freq", type=float, default=100e3)
    a.add_argument("--element-spacing", type=float, default=0.014)
    a.add_argument("--frame-guard-ms", type=float, default=0.2)
    a.add_argument("--tdd-arm-delay-s", type=float, default=0.05)
    a.add_argument("--tdd-rx-timeout-ms", type=int, default=30000)
    a.add_argument("--min-range-m", type=float, default=0.5)
    a.add_argument("--max-range", type=float, default=10.0)
    a.add_argument("--history-len", type=int, default=48)
    a.add_argument("--rx-gain", type=int, default=45)
    a.add_argument("--tx-gain", type=int, default=0)
    a.add_argument("--steer", type=float, default=0.0)
    a.add_argument("--cfar-bias", type=float, default=15.0)
    a.add_argument("--zero-blank-bins", type=int, default=2)
    a.add_argument("--gain-taper", default=",".join(str(g) for g in CHEBYSHEV_TAPER))

    cw = p.add_argument_group("Mode B/C CW interrogation")
    cw.add_argument("--cw-sample-rate", type=float, default=600e3)
    cw.add_argument("--cw-frame-size", type=int, default=8192)
    cw.add_argument("--cw-rx-gain", type=int, default=50)
    cw.add_argument("--cw-tx-gain", type=int, default=-6)
    cw.add_argument("--cw-history-sec", type=float, default=20.0)
    cw.add_argument("--cw-phase-fs", type=float, default=100.0)
    cw.add_argument("--vitals-warmup-sec", type=float, default=8.0)
    cw.add_argument("--min-resp-conf", type=float, default=0.05)
    cw.add_argument("--min-hr-conf", type=float, default=0.12)
    cw.add_argument("--myo-phase-fs", type=float, default=2000.0)
    cw.add_argument("--myo-history-sec", type=float, default=10.0)
    cw.add_argument("--myo-baseline-sec", type=float, default=3.0)
    cw.add_argument("--myo-win-sec", type=float, default=0.25)
    cw.add_argument("--myo-band-lo", type=float, default=10.0)
    cw.add_argument("--myo-band-hi", type=float, default=500.0)
    cw.add_argument("--myo-threshold", type=float, default=0.3)

    loop = p.add_argument_group("closed-loop policy")
    loop.add_argument("--auto-interrogate", action=argparse.BooleanOptionalAction, default=False,
                      help="Automatically switch to CW Modes B/C after target lock. Default is manual for bring-up safety.")
    loop.add_argument("--lock-min-frames", type=int, default=8)
    loop.add_argument("--lock-range-std-m", type=float, default=0.35)
    loop.add_argument("--lock-min-score", type=float, default=0.6,
                      help="Profile margin in log10 units above local background")
    loop.add_argument("--cw-dwell-s", type=float, default=25.0)
    loop.add_argument("--cw-cooldown-s", type=float, default=20.0)
    loop.add_argument("--reopen-delay-s", type=float, default=0.75)

    cal = p.add_argument_group("terminal calibration")
    cal.add_argument("--skip-calibration", action="store_true")
    cal.add_argument("--load-calibration", type=Path, default=None)
    cal.add_argument("--cal-dir", type=Path, default=Path("calibrations"))
    cal.add_argument("--hb100-cal", choices=["auto", "skip", "force"], default="auto",
                     help="Optional HB100 frequency sweep. 'auto' reuses --hb100-cal-file if present.")
    cal.add_argument("--hb100-cal-file", type=Path, default=Path("hb100_freq_val.pkl"))
    cal.add_argument("--hb100-rpi-uri", default="ip:phaser.local",
                     help="CN0566 URI for HB100 calibration. Defaults to ADI reference topology.")
    cal.add_argument("--hb100-sdr-uri", default="ip:phaser.local:50901",
                     help="Pluto URI for HB100 calibration. This intentionally differs from --sdr-uri.")
    cal.add_argument("--hb100-topology", choices=["auto", "reference", "direct"], default="auto",
                     help="HB100 connection strategy. auto tries reference-forwarded then SENTINEL direct.")
    cal.add_argument("--hb100-sweep-start", type=float, default=10.0e9)
    cal.add_argument("--hb100-sweep-stop", type=float, default=10.7e9)
    cal.add_argument("--hb100-sweep-step", type=float, default=10e6)
    cal.add_argument("--hb100-sample-rate", type=float, default=30e6)
    cal.add_argument("--hb100-rx-lo", type=float, default=2.0e9)
    cal.add_argument("--hb100-rx-buffer-size", type=int, default=4 * 256)
    cal.add_argument("--hb100-low-rate-retry", action=argparse.BooleanOptionalAction, default=True,
                     help="If the 30 MSPS HB100 sweep trips IIO, retry at 4 MSPS with a finer sweep step.")
    cal.add_argument("--hb100-retry-sample-rate", type=float, default=4e6)
    cal.add_argument("--hb100-retry-sweep-step", type=float, default=1e6)
    cal.add_argument("--hb100-hard-fail", action="store_true",
                     help="Abort if optional HB100 calibration fails instead of continuing to scene calibration.")
    cal.add_argument("--hb100-rx-gain", type=int, default=0)
    cal.add_argument("--empty-frames", type=int, default=8)
    cal.add_argument("--target-frames", type=int, default=3)
    cal.add_argument("--beam-sweep", default="-45,-30,-15,0,15,30,45",
                     help="Comma-separated steering angles for target calibration")
    cal.add_argument("--cal-target-min-m", type=float, default=0.4)
    cal.add_argument("--cal-target-max-m", type=float, default=6.0)
    cal.add_argument("--cal-expected-range-m", type=float, default=None,
                     help="Approximate target distance during scene calibration. If omitted, the terminal asks.")
    cal.add_argument("--cal-range-penalty", type=float, default=0.6,
                     help="Penalty per metre away from expected target range when choosing beam.")
    cal.add_argument("--cal-angle-penalty", type=float, default=0.004,
                     help="Small penalty per degree away from boresight to avoid choosing side clutter ties.")

    return p


def _parse_taper(text: str) -> tuple[int, ...]:
    vals = tuple(int(v.strip()) for v in text.split(",") if v.strip())
    if len(vals) != 8:
        raise ValueError("--gain-taper must contain exactly 8 integers")
    return vals


def _parse_beam_sweep(text: str) -> list[float]:
    vals = [float(v.strip()) for v in text.split(",") if v.strip()]
    return vals or [0.0]


def _mode_a_hw(args: argparse.Namespace, *, rx_gain: int | None = None) -> HardwareParams:
    return HardwareParams(
        rpi_uri=args.rpi_uri,
        sdr_uri=args.sdr_uri,
        tdd_uri=args.tdd_uri,
        sample_rate=args.sample_rate,
        center_freq=args.center_freq,
        signal_freq=args.signal_freq,
        rx_gain=int(args.rx_gain if rx_gain is None else rx_gain),
        tx_gain=int(args.tx_gain),
        output_freq=args.output_freq,
        chirp_bw=args.bw,
        ramp_time_us=int(args.ramp_time_us),
        num_chirps=int(args.num_chirps),
        gain_taper=_parse_taper(args.gain_taper),
        element_spacing=args.element_spacing,
        tdd_trigger=args.tdd_trigger,
        tdd_sync_external=(args.tdd_trigger == "external"),
        tdd_ext_capture=(args.tdd_trigger == "external"),
        tdd_arm_delay_s=args.tdd_arm_delay_s,
        tdd_rx_timeout_ms=args.tdd_rx_timeout_ms,
        frame_guard_ms=args.frame_guard_ms,
    )


def _open_mode_a_backend(args: argparse.Namespace, steer: float):
    backend = make_backend(args.acq, _mode_a_hw(args))
    cfg = backend.connect()
    backend.set_steering_angle(float(steer))
    return backend, cfg


def _adc_clip_stats(cap: CaptureResult) -> tuple[float, float]:
    parts = []
    for ch in (cap.ch0, cap.ch1):
        if ch is None:
            continue
        arr = np.asarray(ch)
        parts.append(np.real(arr).ravel())
        parts.append(np.imag(arr).ravel())
    if not parts:
        vals = np.concatenate([
            np.real(cap.chirp_matrix).ravel(),
            np.imag(cap.chirp_matrix).ravel(),
        ])
    else:
        vals = np.concatenate(parts)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return 0.0, 0.0
    abs_vals = np.abs(vals)
    peak = float(np.percentile(abs_vals, 99.9))
    clip_fraction = float(np.mean(abs_vals >= 4090.0))
    return clip_fraction, peak


def _process_mode_a(
    cap: CaptureResult,
    *,
    min_range_m: float,
    max_range_m: float | None = None,
    cfar_bias: float,
    zero_blank_bins: int,
    apply_mti: bool = True,
    window: bool = True,
    suppress_leakage: bool = True,
) -> ModeAResult:
    cfg = cap.cfg
    range_full = cfg.range_axis()
    pos_mask = range_full >= 0.0
    range_pos = range_full[pos_mask]

    try:
        chirp_mean = cap.chirp_matrix.mean(axis=0)
        w_bl = np.blackman(len(chirp_mean))
        fft_abs = np.abs(np.fft.fftshift(np.fft.fft(chirp_mean * w_bl)))
        fft_dbfs = 20.0 * np.log10(
            np.maximum(fft_abs / (np.sum(w_bl) + 1e-30), 1e-15) / 2.0**11
        )
        beat_fft_pos = fft_dbfs[pos_mask].astype(np.float32)
    except Exception:
        beat_fft_pos = None

    result = process_frame(
        cap.chirp_matrix,
        apply_mti_filter=apply_mti,
        window=window,
        range_axis_m=range_full,
        min_range_m=min_range_m,
        min_scale=None,
        max_scale=None,
    )
    rd = result["rd_map"].copy()

    if suppress_leakage:
        rd_min = float(rd.min())
        zrw = int(max(0, zero_blank_bins))
        zero_bin = int(np.argmin(np.abs(range_full)))
        rd[max(0, zero_bin - zrw) : min(rd.shape[0], zero_bin + zrw + 1), :] = rd_min

        n_dop = rd.shape[1]
        half = n_dop // 2
        leakage_offset = int(round(cfg.signal_freq * cfg.ramp_time_s * cfg.num_chirps))
        if leakage_offset > 0:
            for sign in (+1, -1):
                col_c = half + sign * leakage_offset
                rd[:, max(0, col_c - 3) : min(n_dop, col_c + 4)] = rd_min

    rd_pos = rd[pos_mask, :]
    profile = rd_pos.max(axis=1).astype(np.float32)

    cfar_threshold = None
    candidate_indices: np.ndarray
    try:
        thresh, _ = _cfar(profile, num_guard_cells=3, num_ref_cells=8, bias=float(cfar_bias))
        cfar_threshold = np.ma.filled(thresh, profile.min()).astype(np.float32)
        range_mask = range_pos >= max(min_range_m, cfg.range_res_m)
        if max_range_m is not None:
            range_mask &= range_pos <= float(max_range_m)
        candidate_indices = np.flatnonzero(
            range_mask & (profile > cfar_threshold)
        )
    except Exception:
        candidate_indices = np.array([], dtype=int)

    if candidate_indices.size:
        order = candidate_indices[np.argsort(profile[candidate_indices])[::-1]]
        sel_bin = int(order[0])
    else:
        mask = range_pos >= max(min_range_m, cfg.range_res_m)
        if max_range_m is not None:
            mask &= range_pos <= float(max_range_m)
        valid = np.flatnonzero(mask)
        sel_bin = int(valid[np.argmax(profile[valid])]) if valid.size else 0
        order = np.array([sel_bin], dtype=int)

    range_m = float(range_pos[sel_bin]) if range_pos.size else 0.0
    floor_mask = range_pos >= max(min_range_m, cfg.range_res_m)
    if max_range_m is not None:
        floor_mask &= range_pos <= float(max_range_m)
    local_floor = (
        float(np.median(profile[floor_mask]))
        if range_pos.size and np.any(floor_mask)
        else float(np.median(profile)) if profile.size else 0.0
    )
    score = float(profile[sel_bin] - local_floor)
    micro = rd_pos[sel_bin, :].astype(np.float32)

    candidates: list[tuple[float, float]] = []
    used: list[int] = []
    min_sep_bins = max(2, int(round(0.6 / max(cfg.range_res_m, 1e-6))))
    for idx in order:
        idx = int(idx)
        if any(abs(idx - u) < min_sep_bins for u in used):
            continue
        used.append(idx)
        candidates.append((float(range_pos[idx]), float(profile[idx] - local_floor)))
        if len(candidates) >= 3:
            break

    return ModeAResult(
        rd_pos=rd_pos,
        profile=profile,
        beat_fft_pos=beat_fft_pos,
        cfar_threshold=cfar_threshold,
        range_m=range_m,
        range_bin=sel_bin,
        target_score=score,
        micro_doppler=micro,
        candidates=candidates,
    )


def _capture_frames(backend, n: int) -> list[CaptureResult]:
    frames: list[CaptureResult] = []
    for i in range(max(1, int(n))):
        print(f"  capture {i + 1}/{n}")
        frames.append(backend.capture())
    return frames


def _write_calibration(args: argparse.Namespace, cal: CalibrationResult) -> CalibrationResult:
    args.cal_dir.mkdir(parents=True, exist_ok=True)
    path = args.cal_dir / f"sentinel_cal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    cal.path = str(path)
    path.write_text(json.dumps(asdict(cal), indent=2), encoding="utf-8")
    print(f"[cal] Saved calibration: {path}")
    return cal


def _load_calibration(path: Path) -> CalibrationResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("hb100_freq_hz", None)
    payload.setdefault("hb100_cal_path", None)
    return CalibrationResult(**payload)


def _load_hb100_freq(path: Path) -> float | None:
    try:
        with path.open("rb") as f:
            return float(pickle.load(f))
    except Exception:
        return None


def _hb100_connection_candidates(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []

    def add(label: str, rpi_uri: str, sdr_uri: str) -> None:
        item = (label, rpi_uri, sdr_uri)
        if item not in candidates:
            candidates.append(item)

    if args.hb100_topology in {"auto", "reference"}:
        add("reference-local", "ip:localhost", args.hb100_sdr_uri)
        add("reference-rpi", args.hb100_rpi_uri, args.hb100_sdr_uri)
    if args.hb100_topology in {"auto", "direct"}:
        add("sentinel-direct", args.rpi_uri, args.sdr_uri)
        if args.tdd_uri and args.tdd_uri != args.sdr_uri:
            add("sentinel-tdd-uri", args.rpi_uri, args.tdd_uri)
    return candidates


def _run_hb100_frequency_sweep_once(
    args: argparse.Namespace,
    *,
    label: str,
    rpi_uri: str,
    sdr_uri: str,
) -> float:
    from adi import ad9361
    from adi.cn0566 import CN0566

    print(f"[hb100] trying {label}: CN0566={rpi_uri}  Pluto={sdr_uri}")
    sdr = None
    phaser = None
    try:
        sdr = ad9361(uri=sdr_uri)
        sdr._ctx.set_timeout(30000)
        phaser = CN0566(uri=rpi_uri, sdr=sdr)
        phaser.sdr = sdr
        phaser.configure(device_mode="rx")
        try:
            phaser.element_spacing = args.element_spacing
        except Exception:
            pass

        for name, value in (
            ("adi,frequency-division-duplex-mode-enable", "1"),
            ("adi,ensm-enable-txnrx-control-enable", "0"),
            ("initialize", "1"),
        ):
            try:
                sdr._ctrl.debug_attrs[name].value = value
            except Exception:
                pass

        sdr.rx_enabled_channels = [0, 1]
        try:
            sdr._rxadc.set_kernel_buffers_count(1)
        except Exception:
            pass
        try:
            sdr._ctrl.find_channel("voltage0").attrs["quadrature_tracking_en"].value = "1"
        except Exception:
            pass

        sdr.sample_rate = int(args.hb100_sample_rate)
        sdr.rx_buffer_size = int(args.hb100_rx_buffer_size)
        try:
            sdr.rx_rf_bandwidth = int(min(10e6, args.hb100_sample_rate))
        except Exception:
            pass
        sdr.rx_lo = int(args.hb100_rx_lo)
        sdr.gain_control_mode_chan0 = "manual"
        sdr.gain_control_mode_chan1 = "manual"
        sdr.rx_hardwaregain_chan0 = int(args.hb100_rx_gain)
        sdr.rx_hardwaregain_chan1 = int(args.hb100_rx_gain)
        try:
            sdr.tx_enabled_channels = [0, 1]
            sdr.tx_cyclic_buffer = False
            sdr.tx_hardwaregain_chan0 = -80
            sdr.tx_hardwaregain_chan1 = -80
        except Exception:
            pass
        try:
            sdr.filter = "LTE20_MHz.ftr"
        except Exception:
            pass

        for i in range(8):
            phaser.set_chan_gain(i, 64, apply_cal=False)
        phaser.set_beam_phase_diff(0.0)
        try:
            phaser.Averages = 8
        except Exception:
            pass
        try:
            phaser.SignalFreq = 10.525e9
            phaser.lo = int(phaser.SignalFreq) + int(sdr.rx_lo)
        except Exception:
            pass

        best_freq = 0.0
        best_amp = -np.inf
        freqs = np.arange(args.hb100_sweep_start, args.hb100_sweep_stop, args.hb100_sweep_step)
        for idx, freq in enumerate(freqs, start=1):
            print(f"[hb100] sweep {idx}/{len(freqs)}  LO={freq/1e9:.3f} GHz")
            try:
                phaser.SignalFreq = float(freq)
            except Exception:
                pass
            try:
                phaser.lo = int(float(freq) + float(sdr.rx_lo))
            except Exception:
                pass
            phaser.frequency = int((float(freq) + float(sdr.rx_lo)) / 4)
            time.sleep(0.05)
            data = sdr.rx()
            if isinstance(data, (list, tuple)):
                x = np.asarray(data[0], dtype=np.complex64) + np.asarray(data[1], dtype=np.complex64)
            else:
                x = np.asarray(data, dtype=np.complex64).reshape(-1)
            win = np.blackman(x.size)
            sp = np.fft.fftshift(np.abs(np.fft.fft(x * win)))
            axis = np.fft.fftshift(np.fft.fftfreq(x.size, 1.0 / float(args.hb100_sample_rate)))
            peak = int(np.argmax(sp))
            if float(sp[peak]) > best_amp:
                best_amp = float(sp[peak])
                best_freq = float(freq + axis[peak])

        if best_freq <= 0.0:
            raise RuntimeError("HB100 sweep did not find a valid peak")
        return best_freq
    finally:
        if sdr is not None:
            try:
                sdr.tx_destroy_buffer()
            except Exception:
                pass
            try:
                sdr.rx_destroy_buffer()
            except Exception:
                pass
        del sdr
        del phaser
        gc.collect()


def _run_hb100_frequency_calibration_adaptive(args: argparse.Namespace) -> float:
    print("[hb100] Running HB100 frequency sweep. This uses the external HB100 source, not onboard TX.")
    last_exc: Exception | None = None
    for label, rpi_uri, sdr_uri in _hb100_connection_candidates(args):
        if (
            args.hb100_topology == "auto"
            and label.startswith("sentinel-")
            and args.hb100_sample_rate > args.hb100_retry_sample_rate
        ):
            print(
                f"[hb100] skipping {label} at {args.hb100_sample_rate/1e6:.1f} MSPS; "
                "direct Pluto is tried only on the low-rate retry."
            )
            continue
        try:
            best_freq = _run_hb100_frequency_sweep_once(
                args,
                label=label,
                rpi_uri=rpi_uri,
                sdr_uri=sdr_uri,
            )
            path = args.hb100_cal_file
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as f:
                pickle.dump(best_freq, f)
            print(f"[hb100] saved HB100 frequency: {best_freq/1e9:.6f} GHz -> {path}")
            return best_freq
        except Exception as exc:
            last_exc = exc
            print(f"[hb100] {label} failed: {type(exc).__name__}: {exc}")
            time.sleep(0.5)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No HB100 connection candidates were configured")


def _run_hb100_frequency_calibration(args: argparse.Namespace) -> float | None:
    """Optional HB100 frequency sweep, cached like ADI's phaser_find_hb100.py."""
    path = args.hb100_cal_file
    if args.hb100_cal == "skip":
        print("[hb100] skipped")
        return _load_hb100_freq(path)
    if path.exists() and args.hb100_cal != "force":
        freq = _load_hb100_freq(path)
        if freq is not None:
            print(f"[hb100] loaded cached HB100 frequency: {freq/1e9:.6f} GHz from {path}")
            return freq
        print(f"[hb100] cache exists but could not be read: {path}")

    if args.hb100_cal == "auto":
        ans = input(
            "\nOptional HB100 kit frequency calibration file is missing.\n"
            "Place HB100 at boresight if available. Run HB100 sweep now? [y/N] "
        ).strip().lower()
        if ans not in {"y", "yes"}:
            print("[hb100] not run; continuing with normal onboard-TX SENTINEL calibration")
            return None

    return _run_hb100_frequency_calibration_adaptive(args)


def _run_hb100_frequency_calibration_safe(args: argparse.Namespace) -> float | None:
    """Run optional HB100 calibration without blocking normal SENTINEL bring-up."""
    try:
        return _run_hb100_frequency_calibration(args)
    except Exception as exc:
        print(f"[hb100] FAILED at {args.hb100_sample_rate/1e6:.1f} MSPS: {type(exc).__name__}: {exc}")
        if args.hb100_hard_fail:
            raise

    if args.hb100_low_rate_retry and args.hb100_sample_rate > args.hb100_retry_sample_rate:
        orig_sample_rate = args.hb100_sample_rate
        orig_sweep_step = args.hb100_sweep_step
        orig_topology = args.hb100_topology
        try:
            args.hb100_sample_rate = float(args.hb100_retry_sample_rate)
            args.hb100_sweep_step = float(min(args.hb100_sweep_step, args.hb100_retry_sweep_step))
            args.hb100_topology = "direct"
            print(
                "[hb100] Retrying at "
                f"{args.hb100_sample_rate/1e6:.1f} MSPS, "
                f"step={args.hb100_sweep_step/1e6:.1f} MHz, "
                "direct topology only."
            )
            time.sleep(1.0)
            return _run_hb100_frequency_calibration(args)
        except Exception as exc:
            print(f"[hb100] low-rate retry failed: {type(exc).__name__}: {exc}")
            if args.hb100_hard_fail:
                raise
        finally:
            args.hb100_sample_rate = orig_sample_rate
            args.hb100_sweep_step = orig_sweep_step
            args.hb100_topology = orig_topology

    cached = _load_hb100_freq(args.hb100_cal_file)
    if cached is not None:
        print(f"[hb100] using last cached value despite sweep failure: {cached/1e9:.6f} GHz")
        return cached
    print("[hb100] continuing without HB100 calibration; normal SENTINEL room calibration will run next.")
    return None


def run_terminal_calibration(args: argparse.Namespace) -> CalibrationResult:
    if args.load_calibration is not None:
        cal = _load_calibration(args.load_calibration)
        print(f"[cal] Loaded calibration: {args.load_calibration}")
        return cal
    if args.skip_calibration:
        print("[cal] Skipped by --skip-calibration")
        return CalibrationResult.default(args)

    hb100_freq = _run_hb100_frequency_calibration_safe(args)

    print("\nSENTINEL calibration")
    print("--------------------")
    print("This second step calibrates the actual room/target path used by SENTINEL.")
    if hb100_freq is not None:
        print(f"HB100 cache available: {hb100_freq/1e9:.6f} GHz")
    print()

    input("1) Clear the monitored area. Press ENTER when the room is empty...")
    backend, _cfg = _open_mode_a_backend(args, args.steer)
    empty_frames: list[CaptureResult] = []
    try:
        empty_frames = _capture_frames(backend, args.empty_frames)
        clip_vals = [_adc_clip_stats(cap) for cap in empty_frames]
        clip_fraction = float(np.mean([c for c, _ in clip_vals]))
        adc_peak = float(np.max([p for _, p in clip_vals]))
        empty_results = [
            _process_mode_a(
                cap,
                min_range_m=args.min_range_m,
                cfar_bias=args.cfar_bias,
                zero_blank_bins=args.zero_blank_bins,
                max_range_m=args.cal_target_max_m,
            )
            for cap in empty_frames
        ]
        rd_vals = np.concatenate([r.rd_pos.ravel() for r in empty_results])
        rd_floor, rd_ceil = np.percentile(rd_vals, [5.0, 99.5])

        notes: list[str] = []
        if clip_fraction > 0.001:
            notes.append(
                f"ADC clipping detected ({clip_fraction:.3%}); reduce --rx-gain by 5-10 dB."
            )
        elif adc_peak < 300:
            notes.append(
                f"ADC peak is low ({adc_peak:.0f}); consider increasing --rx-gain if target is weak."
            )
        else:
            notes.append("ADC level looks usable.")

        print(f"[cal] Empty-scene ADC peak p99.9={adc_peak:.0f}, clipping={clip_fraction:.4%}")
        print(f"[cal] Suggested RD levels: floor={rd_floor:.2f}, ceil={rd_ceil:.2f}")

        input("\n2) Put the target person at the intended monitoring position. Press ENTER...")
        if args.cal_expected_range_m is None:
            text = input("Approx target range in metres for this calibration [blank = unknown]: ").strip()
            if text:
                try:
                    args.cal_expected_range_m = float(text)
                except ValueError:
                    print("[cal] Could not parse range; continuing without expected-range penalty.")
                    args.cal_expected_range_m = None
        best_angle = float(args.steer)
        best_range: float | None = None
        best_score = -1e9
        best_raw_score = -1e9
        for angle in _parse_beam_sweep(args.beam_sweep):
            print(f"[cal] Beam {angle:+.1f} deg")
            backend.set_steering_angle(angle)
            time.sleep(0.1)
            frames = _capture_frames(backend, args.target_frames)
            results = [
                _process_mode_a(
                    cap,
                    min_range_m=args.min_range_m,
                    max_range_m=args.cal_target_max_m,
                    cfar_bias=args.cfar_bias,
                    zero_blank_bins=args.zero_blank_bins,
                )
                for cap in frames
            ]
            raw_score = float(np.median([r.target_score for r in results]))
            rng = float(np.median([r.range_m for r in results]))
            adjusted = raw_score - abs(float(angle)) * float(args.cal_angle_penalty)
            if args.cal_expected_range_m is not None:
                adjusted -= abs(rng - float(args.cal_expected_range_m)) * float(args.cal_range_penalty)
            print(f"      target range={rng:.2f} m  score={raw_score:.2f}  adjusted={adjusted:.2f}")
            if adjusted > best_score:
                best_score = adjusted
                best_raw_score = raw_score
                best_angle = float(angle)
                best_range = rng

        backend.set_steering_angle(best_angle)
        notes.append(f"Best coarse beam angle: {best_angle:+.1f} deg (adjusted score {best_score:.2f}).")
        if best_range is not None:
            notes.append(f"Initial target range: {best_range:.2f} m.")
        if args.cal_expected_range_m is not None:
            notes.append(f"Expected calibration range: {args.cal_expected_range_m:.2f} m.")

        cal = CalibrationResult(
            created_at=datetime.now().isoformat(timespec="seconds"),
            steer_deg=best_angle,
            target_range_m=best_range,
            target_score=float(best_raw_score),
            rx_gain_db=int(args.rx_gain),
            clip_fraction=clip_fraction,
            adc_peak=adc_peak,
            rd_floor=float(rd_floor),
            rd_ceil=float(rd_ceil),
            notes=notes,
            hb100_freq_hz=hb100_freq,
            hb100_cal_path=str(args.hb100_cal_file),
        )
        args.steer = best_angle
        return _write_calibration(args, cal)
    finally:
        try:
            backend.close()
        except Exception:
            pass
        del backend
        gc.collect()


class ModeAWorker(QThread):
    data_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, backend) -> None:
        super().__init__()
        self._backend = backend

    def run(self) -> None:
        while not self.isInterruptionRequested():
            try:
                cap = self._backend.capture()
                self.data_ready.emit(cap)
            except Exception as exc:
                self.error.emit(str(exc))
                break


class CWSession:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sdr = None
        self.phaser = None

    def open(self) -> None:
        import adi

        a = self.args
        self.sdr = adi.ad9361(uri=a.sdr_uri)
        self.sdr._ctx.set_timeout(5000)
        self.phaser = adi.CN0566(uri=a.rpi_uri, sdr=self.sdr)
        self.phaser.configure(device_mode="rx")
        try:
            self.phaser.element_spacing = 0.014
        except Exception:
            pass
        load_phaser_gain_phase_cal(self.phaser)
        for ch in range(8):
            self.phaser.set_chan_phase(ch, 0)
        for ch, g in enumerate(_parse_taper(a.gain_taper)):
            self.phaser.set_chan_gain(ch, g, apply_cal=True)
        for name, val in [("gpio_tx_sw", 0), ("gpio_vctrl_1", 1), ("gpio_vctrl_2", 1)]:
            self._gpio(name, val)

        sdr = self.sdr
        sdr.sample_rate = int(a.cw_sample_rate)
        sdr.rx_lo = int(a.center_freq)
        sdr.rx_enabled_channels = [0, 1]
        sdr.gain_control_mode_chan0 = "manual"
        sdr.gain_control_mode_chan1 = "manual"
        sdr.rx_hardwaregain_chan0 = int(a.cw_rx_gain)
        sdr.rx_hardwaregain_chan1 = int(a.cw_rx_gain)
        sdr.tx_lo = int(a.center_freq)
        sdr.tx_enabled_channels = [0, 1]
        sdr.tx_cyclic_buffer = True
        sdr.tx_hardwaregain_chan0 = -88
        sdr.tx_hardwaregain_chan1 = int(a.cw_tx_gain)

        vco_freq = int(a.output_freq + a.signal_freq + a.center_freq)
        self.phaser.frequency = int(vco_freq / 4)
        self.phaser.ramp_mode = "disabled"
        try:
            self.phaser.tx_trig_en = 0
        except Exception:
            pass
        self.phaser.enable = 0
        self._start_tone()
        sdr.rx_buffer_size = int(a.cw_frame_size)

    def _gpio(self, name: str, val: int) -> None:
        for attr in ("_gpios", "gpios"):
            obj = getattr(self.phaser, attr, None)
            if obj is not None:
                setattr(obj, name, val)
                return

    def _start_tone(self) -> None:
        a = self.args
        n = int(2**18)
        fs = int(a.cw_sample_rate)
        t = np.arange(n) / fs
        iq = (
            0.9
            * (
                np.cos(2 * np.pi * a.signal_freq * t)
                + 1j * np.sin(2 * np.pi * a.signal_freq * t)
            )
            * 2**14
        ).astype(np.complex64)
        try:
            self.sdr.tx_destroy_buffer()
        except Exception:
            pass
        self.sdr.tx([iq, iq])

    def rx(self) -> np.ndarray:
        raw = self.sdr.rx()
        if isinstance(raw, (list, tuple)):
            return np.asarray(raw[0], dtype=np.complex64) + np.asarray(raw[1], dtype=np.complex64)
        if hasattr(raw, "ndim") and raw.ndim > 1:
            return raw[0].astype(np.complex64) + raw[1].astype(np.complex64)
        return np.asarray(raw, dtype=np.complex64).ravel()

    def close(self) -> None:
        if self.sdr is not None:
            try:
                self.sdr.rx_destroy_buffer()
            except Exception:
                pass
            try:
                self.sdr.tx_destroy_buffer()
            except Exception:
                pass
        self.sdr = None
        self.phaser = None
        gc.collect()


class CWWorker(QThread):
    frame_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, session: CWSession) -> None:
        super().__init__()
        self._session = session

    def run(self) -> None:
        while not self.isInterruptionRequested():
            try:
                self.frame_ready.emit(self._session.rx())
            except Exception as exc:
                self.error.emit(str(exc))
                break


class ClosedLoopWindow(QMainWindow):
    def __init__(self, args: argparse.Namespace, cal: CalibrationResult) -> None:
        super().__init__()
        self._args = args
        self._cal = cal
        self._state = "SEARCH"
        self._last_cw_time = -1e9
        self._mode_a_backend = None
        self._mode_a_worker: ModeAWorker | None = None
        self._cw_session: CWSession | None = None
        self._cw_worker: CWWorker | None = None

        self._tracker = TargetTracker(args.lock_min_frames, args.lock_range_std_m, args.lock_min_score)
        self._manual_lock_range_m: float | None = None
        self._last_mode_a: ModeAResult | None = None
        self._last_vitals: VitalsResult | None = None
        self._last_myo: MyoResult | None = None
        self._signatures: list[PersonSignature] = []
        self._cw_div = 0
        self._myo_div = 0

        self._cw_proc = CWProcessor(
            fs=args.cw_sample_rate,
            signal_freq=args.signal_freq,
            rf_freq=args.output_freq,
            history_sec=args.cw_history_sec,
            phase_fs=args.cw_phase_fs,
            warmup_sec=args.vitals_warmup_sec,
        )
        self._myo_proc = MyoProcessor(
            fs=args.cw_sample_rate,
            signal_freq=args.signal_freq,
            phase_fs=args.myo_phase_fs,
            history_sec=args.myo_history_sec,
            baseline_sec=args.myo_baseline_sec,
            win_sec=args.myo_win_sec,
            band_lo=args.myo_band_lo,
            band_hi=args.myo_band_hi,
            threshold=args.myo_threshold,
        )
        self._myo_trace = collections.deque([0.0] * 200, maxlen=200)
        self._myo_t = np.linspace(-args.myo_history_sec, 0.0, 200, dtype=np.float32)

        self._mode_a_backend, self._cfg = _open_mode_a_backend(args, cal.steer_deg)
        self._range_full = self._cfg.range_axis()
        self._pos_mask = self._range_full >= 0.0
        self._range_pos = self._range_full[self._pos_mask]
        self._vel_ms = self._cfg.velocity_axis()
        self._history = MicroDopplerHistory(args.history_len, self._cfg.num_chirps)
        self._last_rd: np.ndarray | None = None

        self.setWindowTitle(
            f"SENTINEL Closed Loop  RF={args.output_freq/1e9:.3f}GHz  "
            f"A={args.num_chirps} chirps/{args.sample_rate/1e6:.1f}MSPS  "
            f"CW={args.cw_sample_rate/1e3:.0f}kSPS"
        )
        self._build_ui()
        self._start_mode_a_worker()
        self.showMaximized()

    def _build_ui(self) -> None:
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidget(self._build_controls())
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(300)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(scroll, stretch=0)
        lay.addWidget(self._build_plots(), stretch=1)
        self.setCentralWidget(root)

    def _build_controls(self) -> QWidget:
        box = QGroupBox("Closed Loop")
        box.setFixedWidth(280)
        lay = QVBoxLayout(box)
        lay.setSpacing(5)

        def lbl(text: str, small: bool = False) -> QLabel:
            w = QLabel(text)
            w.setStyleSheet(f"font-size: {'10' if small else '11'}px; color: #ccc;")
            w.setWordWrap(True)
            return w

        def slider(lo: int, hi: int, val: int, tick: int = 0) -> QSlider:
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(int(val))
            if tick:
                s.setTickInterval(tick)
                s.setTickPosition(QSlider.TicksBelow)
            return s

        self._state_lbl = QLabel("SEARCH")
        self._state_lbl.setAlignment(Qt.AlignCenter)
        self._state_lbl.setStyleSheet("font-size: 18px; font-weight: bold; background:#224466; color:#aaeeff; padding:8px;")
        lay.addWidget(self._state_lbl)

        lock_box = QGroupBox("Target / Lock Policy")
        ll = QVBoxLayout(lock_box)
        ll.setSpacing(3)
        self._auto_cb = QCheckBox("Auto CW interrogation")
        self._auto_cb.setChecked(bool(self._args.auto_interrogate))
        self._auto_cb.stateChanged.connect(self._on_auto_interrogate_changed)
        ll.addWidget(self._auto_cb)

        lock_now = QPushButton("Lock current target")
        lock_now.clicked.connect(self._lock_current_target)
        ll.addWidget(lock_now)

        reset_lock = QPushButton("Unlock / reset target")
        reset_lock.clicked.connect(self._unlock_target)
        ll.addWidget(reset_lock)

        cw_now = QPushButton("Interrogate locked target")
        cw_now.clicked.connect(self._interrogate_locked_target)
        ll.addWidget(cw_now)

        force_cw = QPushButton("Force CW now")
        force_cw.clicked.connect(self._begin_cw_interrogation)
        ll.addWidget(force_cw)

        ll.addWidget(lbl("CW dwell (s)", True))
        self._cwdwell_sl = slider(5, 90, int(self._args.cw_dwell_s), 5)
        self._cwdwell_lbl = lbl(f"{self._args.cw_dwell_s:.0f} s", True)
        self._cwdwell_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "cw_dwell_s", float(v)), self._cwdwell_lbl.setText(f"{v} s"))
        )
        ll.addWidget(self._cwdwell_sl); ll.addWidget(self._cwdwell_lbl)

        ll.addWidget(lbl("CW cooldown (s)", True))
        self._cool_sl = slider(0, 120, int(self._args.cw_cooldown_s), 10)
        self._cool_lbl = lbl(f"{self._args.cw_cooldown_s:.0f} s", True)
        self._cool_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "cw_cooldown_s", float(v)), self._cool_lbl.setText(f"{v} s"))
        )
        ll.addWidget(self._cool_sl); ll.addWidget(self._cool_lbl)
        lay.addWidget(lock_box)

        beam_box = QGroupBox("Beam / Gains")
        bl = QVBoxLayout(beam_box)
        bl.setSpacing(3)
        bl.addWidget(lbl("Steering (deg)", True))
        self._steer_sl = slider(-80, 80, int(self._cal.steer_deg), 20)
        self._steer_lbl = lbl(f"{self._cal.steer_deg:+.0f} deg", True)
        self._steer_sl.valueChanged.connect(self._on_steer)
        bl.addWidget(self._steer_sl); bl.addWidget(self._steer_lbl)

        bl.addWidget(lbl("Mode A RX gain (dB)", True))
        self._rxgain_sl = slider(-3, 70, int(self._args.rx_gain), 10)
        self._rxgain_lbl = lbl(f"{self._args.rx_gain} dB", True)
        self._rxgain_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "rx_gain", int(v)), self._rxgain_lbl.setText(f"{v} dB"))
        )
        bl.addWidget(self._rxgain_sl); bl.addWidget(self._rxgain_lbl)

        bl.addWidget(lbl("Mode A TX gain (dB)", True))
        self._txgain_sl = slider(-30, 0, int(self._args.tx_gain), 5)
        self._txgain_lbl = lbl(f"{self._args.tx_gain} dB", True)
        self._txgain_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "tx_gain", int(v)), self._txgain_lbl.setText(f"{v} dB"))
        )
        bl.addWidget(self._txgain_sl); bl.addWidget(self._txgain_lbl)

        apply_a_gain = QPushButton("Apply Mode A gains")
        apply_a_gain.clicked.connect(self._apply_mode_a_gains)
        bl.addWidget(apply_a_gain)

        bl.addWidget(lbl("CW RX gain (dB)", True))
        self._cwrx_sl = slider(-3, 70, int(self._args.cw_rx_gain), 10)
        self._cwrx_lbl = lbl(f"{self._args.cw_rx_gain} dB", True)
        self._cwrx_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "cw_rx_gain", int(v)), self._cwrx_lbl.setText(f"{v} dB"))
        )
        bl.addWidget(self._cwrx_sl); bl.addWidget(self._cwrx_lbl)

        bl.addWidget(lbl("CW TX gain (dB)", True))
        self._cwtx_sl = slider(-30, 0, int(self._args.cw_tx_gain), 5)
        self._cwtx_lbl = lbl(f"{self._args.cw_tx_gain} dB", True)
        self._cwtx_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "cw_tx_gain", int(v)), self._cwtx_lbl.setText(f"{v} dB"))
        )
        bl.addWidget(self._cwtx_sl); bl.addWidget(self._cwtx_lbl)

        apply_cw_gain = QPushButton("Apply CW gains")
        apply_cw_gain.clicked.connect(self._apply_cw_gains)
        bl.addWidget(apply_cw_gain)
        lay.addWidget(beam_box)

        fmcw_box = QGroupBox("Mode A FMCW / DSP")
        fl = QVBoxLayout(fmcw_box)
        fl.setSpacing(3)

        fl.addWidget(lbl("Bandwidth (MHz)", True))
        self._bw_sl = slider(100, 500, int(self._args.bw / 1e6), 50)
        self._bw_lbl = lbl(f"{self._args.bw/1e6:.0f} MHz", True)
        self._bw_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "bw", float(v) * 1e6), self._bw_lbl.setText(f"{v} MHz"))
        )
        fl.addWidget(self._bw_sl); fl.addWidget(self._bw_lbl)

        fl.addWidget(lbl("Ramp time (us)", True))
        self._ramp_sl = slider(100, 1000, int(self._args.ramp_time_us), 100)
        self._ramp_lbl = lbl(f"{self._args.ramp_time_us} us", True)
        self._ramp_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "ramp_time_us", int(v)), self._ramp_lbl.setText(f"{v} us"))
        )
        fl.addWidget(self._ramp_sl); fl.addWidget(self._ramp_lbl)

        fl.addWidget(lbl("Chirps", True))
        self._chirp_sl = slider(16, 256, int(self._args.num_chirps), 32)
        self._chirp_lbl = lbl(f"{self._args.num_chirps}", True)
        self._chirp_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "num_chirps", max(16, (int(v)//16)*16)), self._chirp_lbl.setText(f"{self._args.num_chirps}"))
        )
        fl.addWidget(self._chirp_sl); fl.addWidget(self._chirp_lbl)

        fl.addWidget(lbl("Min target range (cm)", True))
        self._minr_sl = slider(0, 300, int(self._args.min_range_m * 100), 25)
        self._minr_lbl = lbl(f"{self._args.min_range_m:.2f} m", True)
        self._minr_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "min_range_m", float(v) / 100.0), self._minr_lbl.setText(f"{self._args.min_range_m:.2f} m"))
        )
        fl.addWidget(self._minr_sl); fl.addWidget(self._minr_lbl)

        fl.addWidget(lbl("Max display range (m)", True))
        self._maxr_sl = slider(2, 60, int(self._args.max_range), 5)
        self._maxr_sl.valueChanged.connect(self._on_max_range)
        fl.addWidget(self._maxr_sl)
        self._maxr_lbl = lbl(f"{int(self._args.max_range)} m", True)
        fl.addWidget(self._maxr_lbl)

        fl.addWidget(lbl("CFAR bias", True))
        self._cfar_sl = slider(0, 50, int(self._args.cfar_bias), 5)
        self._cfar_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "cfar_bias", float(v)), self._cfar_lbl.setText(f"{v}"))
        )
        fl.addWidget(self._cfar_sl)
        self._cfar_lbl = lbl(f"{int(self._args.cfar_bias)}", True)
        fl.addWidget(self._cfar_lbl)

        fl.addWidget(lbl("Zero blank bins", True))
        self._zrw_sl = slider(0, 20, int(self._args.zero_blank_bins), 2)
        self._zrw_lbl = lbl(f"+/-{self._args.zero_blank_bins}", True)
        self._zrw_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "zero_blank_bins", int(v)), self._zrw_lbl.setText(f"+/-{v}"))
        )
        fl.addWidget(self._zrw_sl); fl.addWidget(self._zrw_lbl)

        self._mti_cb = QCheckBox("MTI clutter filter")
        self._mti_cb.setChecked(True)
        fl.addWidget(self._mti_cb)
        self._win_cb = QCheckBox("Range/Doppler window")
        self._win_cb.setChecked(True)
        fl.addWidget(self._win_cb)
        self._leak_cb = QCheckBox("Suppress TX leakage")
        self._leak_cb.setChecked(True)
        fl.addWidget(self._leak_cb)

        apply_fmcw = QPushButton("Apply/restart Mode A")
        apply_fmcw.clicked.connect(self._apply_mode_a_params)
        fl.addWidget(apply_fmcw)
        lay.addWidget(fmcw_box)

        display_box = QGroupBox("Mode A Display")
        dl = QVBoxLayout(display_box)
        dl.setSpacing(3)
        dl.addWidget(lbl("Color floor / ceiling", True))
        self._floor_sl = slider(0, 80, int(np.clip(self._cal.rd_floor * 10, 0, 80)), 10)
        self._floor_sl.setValue(int(np.clip(self._cal.rd_floor * 10, 0, 80)))
        self._ceil_sl = slider(20, 100, int(np.clip(self._cal.rd_ceil * 10, 20, 100)), 10)
        self._ceil_sl.setValue(int(np.clip(self._cal.rd_ceil * 10, 20, 100)))
        dl.addWidget(self._floor_sl)
        dl.addWidget(self._ceil_sl)

        auto_level = QPushButton("Auto level A")
        auto_level.clicked.connect(self._on_auto_level)
        dl.addWidget(auto_level)
        lay.addWidget(display_box)

        vit_box = QGroupBox("Mode B Vitals")
        vl = QVBoxLayout(vit_box)
        vl.setSpacing(3)
        vl.addWidget(lbl("Min RR confidence (x100)", True))
        self._rrconf_sl = slider(0, 100, int(self._args.min_resp_conf * 100), 10)
        self._rrconf_lbl = lbl(f"{self._args.min_resp_conf:.2f}", True)
        self._rrconf_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "min_resp_conf", float(v) / 100.0), self._rrconf_lbl.setText(f"{self._args.min_resp_conf:.2f}"))
        )
        vl.addWidget(self._rrconf_sl); vl.addWidget(self._rrconf_lbl)

        vl.addWidget(lbl("Min HR confidence (x100)", True))
        self._hrconf_sl = slider(0, 100, int(self._args.min_hr_conf * 100), 10)
        self._hrconf_lbl = lbl(f"{self._args.min_hr_conf:.2f}", True)
        self._hrconf_sl.valueChanged.connect(
            lambda v: (setattr(self._args, "min_hr_conf", float(v) / 100.0), self._hrconf_lbl.setText(f"{self._args.min_hr_conf:.2f}"))
        )
        vl.addWidget(self._hrconf_sl); vl.addWidget(self._hrconf_lbl)
        reset_cw = QPushButton("Reset CW phase ring")
        reset_cw.clicked.connect(self._reset_cw_processors)
        vl.addWidget(reset_cw)
        lay.addWidget(vit_box)

        myo_box = QGroupBox("Mode C Myo")
        ml = QVBoxLayout(myo_box)
        ml.setSpacing(3)
        ml.addWidget(lbl("Myo threshold (x10)", True))
        self._myothr_sl = slider(1, 10, int(self._args.myo_threshold * 10), 1)
        self._myothr_lbl = lbl(f"{self._args.myo_threshold:.1f}", True)
        self._myothr_sl.valueChanged.connect(self._on_myo_threshold)
        ml.addWidget(self._myothr_sl); ml.addWidget(self._myothr_lbl)

        ml.addWidget(lbl("Band low / high (Hz)", True))
        self._myolo_sl = slider(1, 200, int(self._args.myo_band_lo), 25)
        self._myohi_sl = slider(50, 800, int(self._args.myo_band_hi), 50)
        self._myoband_lbl = lbl(f"{self._args.myo_band_lo:.0f}-{self._args.myo_band_hi:.0f} Hz", True)
        self._myolo_sl.valueChanged.connect(self._on_myo_band)
        self._myohi_sl.valueChanged.connect(self._on_myo_band)
        ml.addWidget(self._myolo_sl); ml.addWidget(self._myohi_sl); ml.addWidget(self._myoband_lbl)
        recal_myo = QPushButton("Recalibrate myo")
        recal_myo.clicked.connect(self._myo_proc.reset_calibration)
        ml.addWidget(recal_myo)
        lay.addWidget(myo_box)

        self._status_lbl = lbl("Waiting for first frame...", True)
        self._status_lbl.setStyleSheet("font-size: 10px; color: #88aaff;")
        lay.addWidget(self._status_lbl)

        self._signature_lbl = lbl("Signature: none", True)
        self._signature_lbl.setStyleSheet("font-size: 10px; color: #ffddaa;")
        lay.addWidget(self._signature_lbl)

        lay.addStretch()
        quit_btn = QPushButton("Quit")
        quit_btn.setStyleSheet("background-color:#662222; color:white; font-weight:bold;")
        quit_btn.clicked.connect(self.close)
        lay.addWidget(quit_btn)
        return box

    def _build_plots(self) -> pg.GraphicsLayoutWidget:
        glw = pg.GraphicsLayoutWidget()
        r_display = float(self._args.max_range)
        v = self._vel_ms
        r = self._range_pos

        p_fft = glw.addPlot(row=0, col=0, title="Mode A Beat/Profile")
        p_fft.setLabel("bottom", "Range [m]", **LABEL_KW)
        p_fft.setLabel("left", "dBFS / profile", **LABEL_KW)
        p_fft.setXRange(0, r_display, padding=0)
        p_fft.setYRange(-100, 0, padding=0)
        p_fft.showGrid(x=True, y=True, alpha=0.2)
        self._fft_curve = p_fft.plot(pen=pg.mkPen((255, 200, 50), width=1.4))
        self._profile_curve = p_fft.plot(pen=pg.mkPen((255, 120, 30), width=1.0, style=Qt.DotLine))
        self._cfar_curve = p_fft.plot(pen=pg.mkPen((50, 200, 255), width=1.0, style=Qt.DashLine))
        self._gate_line_fft = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((80, 255, 120), width=2))
        p_fft.addItem(self._gate_line_fft)

        p_rd = glw.addPlot(row=0, col=1, title="Mode A Range-Doppler")
        p_rd.setLabel("bottom", "Velocity [m/s]", **LABEL_KW)
        p_rd.setLabel("left", "Range [m]", **LABEL_KW)
        p_rd.setXRange(float(v.min()), float(v.max()), padding=0)
        p_rd.setYRange(0, r_display, padding=0)
        p_rd.showGrid(x=True, y=True, alpha=0.15)
        self._rd_img = pg.ImageItem()
        self._rd_img.setLookupTable(INFERNO_LUT)
        self._rd_img.setRect(pg.QtCore.QRectF(float(v.min()), 0.0, float(v.max() - v.min()), float(r.max())))
        p_rd.addItem(self._rd_img)
        self._gate_line_rd = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen((80, 255, 120), width=1.5, style=Qt.DashLine))
        p_rd.addItem(self._gate_line_rd)

        p_vit = glw.addPlot(row=0, col=2, title="Mode B Vitals")
        p_vit.setLabel("bottom", "Time [s]", **LABEL_KW)
        p_vit.setLabel("left", "Displacement [mm]", **LABEL_KW)
        p_vit.setXRange(0, self._args.cw_history_sec, padding=0)
        p_vit.showGrid(x=True, y=True, alpha=0.2)
        self._resp_curve = p_vit.plot(pen=pg.mkPen((0, 255, 200), width=1.4))
        self._hr_curve = p_vit.plot(pen=pg.mkPen((255, 100, 200), width=1.4))

        p_md = glw.addPlot(row=1, col=0, colspan=2, title="Mode A Micro-Doppler History")
        p_md.setLabel("bottom", "Frame index", **LABEL_KW)
        p_md.setLabel("left", "Velocity [m/s]", **LABEL_KW)
        p_md.setXRange(0, self._args.history_len, padding=0)
        p_md.setYRange(float(v.min()), float(v.max()), padding=0)
        p_md.showGrid(x=True, y=True, alpha=0.15)
        self._md_img = pg.ImageItem()
        self._md_img.setLookupTable(VIRIDIS_LUT)
        self._md_img.setRect(pg.QtCore.QRectF(0, float(v.min()), float(self._args.history_len), float(v.max() - v.min())))
        p_md.addItem(self._md_img)

        p_psd = glw.addPlot(row=1, col=2, title="Mode B Phase PSD")
        p_psd.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_psd.setLabel("left", "PSD [dB/Hz]", **LABEL_KW)
        p_psd.setXRange(0, 4, padding=0)
        p_psd.showGrid(x=True, y=True, alpha=0.2)
        self._psd_curve = p_psd.plot(pen=pg.mkPen((255, 200, 50), width=1.4))

        p_myo = glw.addPlot(row=2, col=0, title="Mode C Myo-index")
        p_myo.setLabel("bottom", "Time [s]", **LABEL_KW)
        p_myo.setLabel("left", "Index", **LABEL_KW)
        p_myo.setXRange(-self._args.myo_history_sec, 0, padding=0)
        p_myo.setYRange(0, 1, padding=0)
        p_myo.showGrid(x=True, y=True, alpha=0.2)
        self._myo_curve = p_myo.plot(pen=pg.mkPen((255, 165, 0), width=2))
        p_myo.addItem(pg.InfiniteLine(pos=self._args.myo_threshold, angle=0, movable=False, pen=pg.mkPen((255, 80, 80), style=Qt.DashLine)))

        p_amp = glw.addPlot(row=2, col=1, colspan=2, title="Mode C Amplitude PSD")
        p_amp.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_amp.setLabel("left", "PSD [dB]", **LABEL_KW)
        p_amp.setXRange(0, 600, padding=0)
        p_amp.showGrid(x=True, y=True, alpha=0.2)
        self._amp_curve = p_amp.plot(pen=pg.mkPen((255, 165, 0), width=1.4))

        self._p_fft = p_fft
        self._p_rd = p_rd
        return glw

    def _start_mode_a_worker(self) -> None:
        self._state = "SEARCH"
        self._update_state_label()
        self._mode_a_worker = ModeAWorker(self._mode_a_backend)
        self._mode_a_worker.data_ready.connect(self._on_mode_a_capture)
        self._mode_a_worker.error.connect(self._on_mode_a_error)
        self._mode_a_worker.start()

    def _stop_mode_a(self) -> None:
        if self._mode_a_worker is not None:
            self._mode_a_worker.requestInterruption()
            self._mode_a_worker.wait(5000)
            self._mode_a_worker = None
        if self._mode_a_backend is not None:
            try:
                self._mode_a_backend.close()
            except Exception:
                pass
            self._mode_a_backend = None
            gc.collect()

    def _restart_mode_a(self) -> None:
        try:
            time.sleep(max(0.0, self._args.reopen_delay_s))
            self._mode_a_backend, cfg = _open_mode_a_backend(self._args, self._cal.steer_deg)
            self._refresh_mode_a_context(cfg)
            self._start_mode_a_worker()
            return True
        except Exception as exc:
            self._state = "RX ERROR"
            self._update_state_label()
            self._status_lbl.setText(
                f"Mode A restart failed: {type(exc).__name__}: {exc}\n"
                "Close the app, disable TDD if needed, then retry after the IIO path recovers."
            )
            self._mode_a_backend = None
            return False

    def _refresh_mode_a_context(self, cfg) -> None:
        self._cfg = cfg
        self._range_full = cfg.range_axis()
        self._pos_mask = self._range_full >= 0.0
        self._range_pos = self._range_full[self._pos_mask]
        self._vel_ms = cfg.velocity_axis()
        self._history = MicroDopplerHistory(self._args.history_len, cfg.num_chirps)
        if hasattr(self, "_p_fft"):
            self._p_fft.setXRange(0.0, float(self._args.max_range), padding=0)
        if hasattr(self, "_p_rd"):
            self._p_rd.setXRange(float(self._vel_ms.min()), float(self._vel_ms.max()), padding=0)
            self._p_rd.setYRange(0.0, float(self._args.max_range), padding=0)
        if hasattr(self, "_rd_img"):
            self._rd_img.setRect(pg.QtCore.QRectF(
                float(self._vel_ms.min()),
                0.0,
                float(self._vel_ms.max() - self._vel_ms.min()),
                float(self._range_pos.max()),
            ))
        if hasattr(self, "_md_img"):
            self._md_img.setRect(pg.QtCore.QRectF(
                0,
                float(self._vel_ms.min()),
                float(self._args.history_len),
                float(self._vel_ms.max() - self._vel_ms.min()),
            ))

    def _begin_cw_interrogation(self) -> None:
        if self._state in {"CW", "SWITCHING"}:
            return
        self._state = "SWITCHING"
        self._update_state_label()
        try:
            self._stop_mode_a()
            time.sleep(max(0.0, self._args.reopen_delay_s))
            self._cw_proc = CWProcessor(
                fs=self._args.cw_sample_rate,
                signal_freq=self._args.signal_freq,
                rf_freq=self._args.output_freq,
                history_sec=self._args.cw_history_sec,
                phase_fs=self._args.cw_phase_fs,
                warmup_sec=self._args.vitals_warmup_sec,
            )
            self._myo_proc.reset_calibration()
            self._last_vitals = None
            self._last_myo = None
            self._cw_div = 0
            self._myo_div = 0
            self._cw_session = CWSession(self._args)
            self._cw_session.open()
            self._cw_worker = CWWorker(self._cw_session)
            self._cw_worker.frame_ready.connect(self._on_cw_frame)
            self._cw_worker.error.connect(self._on_cw_error)
            self._state = "CW"
            self._update_state_label()
            self._cw_worker.start()
            QTimer.singleShot(int(self._args.cw_dwell_s * 1000), self._end_cw_interrogation)
        except Exception as exc:
            self._status_lbl.setText(f"CW switch failed: {type(exc).__name__}: {exc}\nRestarting Mode A...")
            self._close_cw_resources()
            self._restart_mode_a()

    def _on_cw_error(self, msg: str) -> None:
        self._status_lbl.setText(f"CW error: {msg}\nReturning to Mode A.")
        self._end_cw_interrogation()

    def _close_cw_resources(self) -> None:
        if self._cw_worker is not None:
            self._cw_worker.requestInterruption()
            self._cw_worker.wait(3000)
            self._cw_worker = None
        if self._cw_session is not None:
            try:
                self._cw_session.close()
            except Exception:
                pass
            self._cw_session = None

    def _end_cw_interrogation(self) -> None:
        was_cw = self._state == "CW"
        self._close_cw_resources()
        if was_cw:
            self._store_signature()
        self._last_cw_time = time.monotonic()
        if self._mode_a_backend is None:
            self._restart_mode_a()

    def _store_signature(self) -> None:
        vitals = self._last_vitals
        if vitals is None:
            return
        myo = self._last_myo
        sig = PersonSignature(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            range_m=float(self._tracker.range_m or 0.0),
            resp_bpm=float(vitals.resp_bpm),
            resp_conf=float(vitals.resp_conf),
            hr_bpm=float(vitals.hr_bpm),
            hr_conf=float(vitals.hr_conf),
            myo_index=(float(myo.myo_index) if myo is not None else None),
            myo_active=(bool(myo.activation) if myo is not None else None),
        )
        self._signatures.append(sig)
        rr = f"{sig.resp_bpm:.1f}" if sig.resp_conf >= self._args.min_resp_conf else "--"
        hr = f"{sig.hr_bpm:.1f}" if sig.hr_conf >= self._args.min_hr_conf else "--"
        myo_txt = "--" if sig.myo_index is None else f"{sig.myo_index:.2f}"
        self._signature_lbl.setText(
            f"Signatures: {len(self._signatures)}  last range={sig.range_m:.2f}m  "
            f"RR={rr}  HR={hr}  myo={myo_txt}"
        )

    def _on_mode_a_capture(self, cap: CaptureResult) -> None:
        t0 = time.monotonic()
        res = _process_mode_a(
            cap,
            min_range_m=self._args.min_range_m,
            max_range_m=self._args.max_range,
            cfar_bias=float(self._cfar_sl.value()),
            zero_blank_bins=self._args.zero_blank_bins,
            apply_mti=self._mti_cb.isChecked(),
            window=self._win_cb.isChecked(),
            suppress_leakage=self._leak_cb.isChecked(),
        )
        self._last_mode_a = res
        self._last_rd = res.rd_pos
        history = self._history.push(res.micro_doppler)
        floor = self._floor_sl.value() / 10.0
        ceil = max(floor + 0.1, self._ceil_sl.value() / 10.0)

        if res.beat_fft_pos is not None:
            self._fft_curve.setData(x=self._range_pos, y=res.beat_fft_pos)
        if res.profile.max() > 0:
            p_norm = (res.profile / (res.profile.max() + 1e-9)) * 30.0 - 80.0
            self._profile_curve.setData(x=self._range_pos, y=p_norm)
        if res.cfar_threshold is not None and res.profile.max() > 0:
            c_norm = (res.cfar_threshold / (res.profile.max() + 1e-9)) * 30.0 - 80.0
            self._cfar_curve.setData(x=self._range_pos, y=c_norm)
        self._gate_line_fft.setValue(res.range_m)
        self._gate_line_rd.setValue(res.range_m)
        self._rd_img.setImage(res.rd_pos, levels=(floor, ceil))
        self._md_img.setImage(history.T, levels=(floor, ceil))

        auto_locked = self._tracker.update(res.range_m, res.target_score)
        now = time.monotonic()
        manual_locked = self._manual_lock_range_m is not None
        locked = bool(auto_locked or manual_locked)
        if manual_locked:
            self._tracker.locked = True
            self._tracker.range_m = float(self._manual_lock_range_m)
            self._tracker.quality = 1.0

        if locked:
            self._state = "LOCKED"
        else:
            self._state = "SEARCH"
        self._update_state_label()

        cooldown_remaining = max(0.0, self._args.cw_cooldown_s - (now - self._last_cw_time))
        if self._args.auto_interrogate and locked and cooldown_remaining <= 0.0:
            self._begin_cw_interrogation()
            return

        dt = (time.monotonic() - t0) * 1000
        cand = ", ".join(f"{r:.1f}m/{s:.1f}" for r, s in res.candidates) or "none"
        lock_src = "manual" if manual_locked else "auto" if auto_locked else "none"
        if locked and self._args.auto_interrogate:
            action = f"auto CW in {cooldown_remaining:.1f}s" if cooldown_remaining > 0 else "starting CW"
        elif locked:
            action = "manual mode: press 'Interrogate locked target' or enable Auto CW"
        else:
            action = "searching"
        self._status_lbl.setText(
            f"target={res.range_m:.2f} m score={res.target_score:.2f} "
            f"lock={lock_src} q={self._tracker.quality:.2f}\n"
            f"candidates: {cand}\n{action}; proc={dt:.0f} ms"
        )

    def _on_cw_frame(self, raw: np.ndarray) -> None:
        self._cw_proc.push(raw)
        self._myo_proc.push(raw)

        self._cw_div += 1
        if self._cw_div >= 10:
            self._cw_div = 0
            r = self._cw_proc.compute()
            if r is not None:
                self._last_vitals = r
                self._resp_curve.setData(x=r.time_axis, y=r.resp_disp_mm)
                self._hr_curve.setData(x=r.time_axis, y=r.hr_disp_mm)
                self._psd_curve.setData(x=r.psd_freq_hz, y=r.psd_db)
                rr = f"{r.resp_bpm:.1f}" if r.resp_conf >= self._args.min_resp_conf else "--"
                hr = f"{r.hr_bpm:.1f}" if r.hr_conf >= self._args.min_hr_conf else "--"
                self._signature_lbl.setText(f"Signature: range={self._tracker.range_m or 0:.2f}m  RR={rr} bpm  HR={hr} bpm")

        self._myo_div += 1
        if self._myo_div >= 4:
            self._myo_div = 0
            m = self._myo_proc.compute()
            if m is not None:
                self._last_myo = m
                self._myo_trace.append(m.myo_index)
                self._myo_curve.setData(x=self._myo_t, y=np.asarray(self._myo_trace, dtype=np.float32))
                self._amp_curve.setData(x=m.spec_freq_hz, y=m.spec_db)

    def _update_state_label(self) -> None:
        colors = {
            "SEARCH": ("#224466", "#aaeeff"),
            "LOCKED": ("#225522", "#aaffaa"),
            "SWITCHING": ("#554422", "#ffeeaa"),
            "CW": ("#444422", "#ffeeaa"),
            "RX ERROR": ("#772222", "#ffcccc"),
        }
        bg, fg = colors.get(self._state, ("#333355", "#ffffff"))
        self._state_lbl.setText(self._state)
        self._state_lbl.setStyleSheet(f"font-size:18px; font-weight:bold; background:{bg}; color:{fg}; padding:8px;")

    def _on_mode_a_error(self, msg: str) -> None:
        self._state = "RX ERROR"
        self._update_state_label()
        self._status_lbl.setText(f"Mode A error: {msg}\nMode A worker stopped; use Apply/restart Mode A after the IIO path recovers.")

    def _on_auto_interrogate_changed(self, state: int) -> None:
        self._args.auto_interrogate = state == Qt.Checked
        if not self._args.auto_interrogate:
            self._status_lbl.setText("Auto CW interrogation disabled. Use 'Interrogate locked target' for B/C.")
            return
        if self._manual_lock_range_m is None and not self._tracker.locked:
            self._status_lbl.setText("Auto CW interrogation enabled. It will start after target lock.")
            return
        cooldown_remaining = max(
            0.0,
            self._args.cw_cooldown_s - (time.monotonic() - self._last_cw_time),
        )
        if cooldown_remaining > 0:
            self._status_lbl.setText(f"Auto CW enabled; cooldown remaining {cooldown_remaining:.1f} s.")
            return
        self._status_lbl.setText("Auto CW enabled on locked target; starting B/C interrogation.")
        QTimer.singleShot(50, self._begin_cw_interrogation)

    def _lock_current_target(self) -> None:
        if self._last_mode_a is None:
            self._status_lbl.setText("No Mode A target candidate yet.")
            return
        self._manual_lock_range_m = float(self._last_mode_a.range_m)
        self._tracker.locked = True
        self._tracker.range_m = self._manual_lock_range_m
        self._tracker.quality = 1.0
        self._state = "LOCKED"
        self._update_state_label()
        if self._args.auto_interrogate:
            cooldown_remaining = max(
                0.0,
                self._args.cw_cooldown_s - (time.monotonic() - self._last_cw_time),
            )
            if cooldown_remaining <= 0:
                self._status_lbl.setText(
                    f"Manual lock set at {self._manual_lock_range_m:.2f} m; starting B/C interrogation."
                )
                QTimer.singleShot(50, self._begin_cw_interrogation)
                return
            self._status_lbl.setText(
                f"Manual lock set at {self._manual_lock_range_m:.2f} m; "
                f"auto B/C after cooldown {cooldown_remaining:.1f} s."
            )
            return
        self._status_lbl.setText(
            f"Manual lock set at {self._manual_lock_range_m:.2f} m. "
            "Press 'Interrogate locked target' to run Modes B/C."
        )

    def _unlock_target(self) -> None:
        self._manual_lock_range_m = None
        self._tracker.reset()
        self._state = "SEARCH"
        self._update_state_label()
        self._status_lbl.setText("Target lock cleared.")

    def _interrogate_locked_target(self) -> None:
        if self._manual_lock_range_m is None and not self._tracker.locked:
            self._status_lbl.setText("No locked target. Use 'Lock current target' first, or enable Auto CW interrogation.")
            return
        self._begin_cw_interrogation()

    def _on_steer(self, value: int) -> None:
        self._args.steer = float(value)
        self._cal.steer_deg = float(value)
        self._steer_lbl.setText(f"{value:+d} deg")
        if self._mode_a_backend is not None:
            try:
                self._mode_a_backend.set_steering_angle(float(value))
            except Exception as exc:
                self._status_lbl.setText(f"Steering failed: {exc}")

    def _apply_mode_a_gains(self) -> None:
        if self._mode_a_backend is None:
            self._status_lbl.setText("Mode A gains will apply next time Mode A starts.")
            return
        sdr = getattr(self._mode_a_backend, "_sdr", None)
        if sdr is None:
            return
        try:
            sdr.rx_hardwaregain_chan0 = int(self._args.rx_gain)
            sdr.rx_hardwaregain_chan1 = int(self._args.rx_gain)
            sdr.tx_hardwaregain_chan1 = int(self._args.tx_gain)
            self._status_lbl.setText(f"Applied Mode A gains: RX={self._args.rx_gain} dB, TX={self._args.tx_gain} dB.")
        except Exception as exc:
            self._status_lbl.setText(f"Mode A gain apply failed: {exc}")

    def _apply_cw_gains(self) -> None:
        if self._cw_session is None or self._cw_session.sdr is None:
            self._status_lbl.setText("CW gains will apply next CW interrogation.")
            return
        try:
            sdr = self._cw_session.sdr
            sdr.rx_hardwaregain_chan0 = int(self._args.cw_rx_gain)
            sdr.rx_hardwaregain_chan1 = int(self._args.cw_rx_gain)
            sdr.tx_hardwaregain_chan1 = int(self._args.cw_tx_gain)
            self._status_lbl.setText(f"Applied CW gains: RX={self._args.cw_rx_gain} dB, TX={self._args.cw_tx_gain} dB.")
        except Exception as exc:
            self._status_lbl.setText(f"CW gain apply failed: {exc}")

    def _apply_mode_a_params(self) -> None:
        self._unlock_target()
        if self._state == "CW":
            self._status_lbl.setText("Mode A parameters will apply after CW returns to Mode A.")
            return
        self._status_lbl.setText("Restarting Mode A with updated FMCW parameters...")
        self._stop_mode_a()
        self._restart_mode_a()

    def _reset_cw_processors(self) -> None:
        self._cw_proc = CWProcessor(
            fs=self._args.cw_sample_rate,
            signal_freq=self._args.signal_freq,
            rf_freq=self._args.output_freq,
            history_sec=self._args.cw_history_sec,
            phase_fs=self._args.cw_phase_fs,
            warmup_sec=self._args.vitals_warmup_sec,
        )
        self._last_vitals = None
        self._cw_div = 0
        self._status_lbl.setText("CW phase/vitals processor reset.")

    def _on_myo_threshold(self, value: int) -> None:
        self._args.myo_threshold = float(value) / 10.0
        self._myo_proc.threshold = self._args.myo_threshold
        self._myothr_lbl.setText(f"{self._args.myo_threshold:.1f}")

    def _on_myo_band(self, _value: int) -> None:
        lo = float(self._myolo_sl.value())
        hi = float(self._myohi_sl.value())
        if hi <= lo:
            hi = lo + 1.0
        self._args.myo_band_lo = lo
        self._args.myo_band_hi = hi
        self._myo_proc.band_lo = lo
        self._myo_proc.band_hi = hi
        self._myoband_lbl.setText(f"{lo:.0f}-{hi:.0f} Hz")

    def _on_max_range(self, value: int) -> None:
        self._args.max_range = float(value)
        self._maxr_lbl.setText(f"{value} m")
        self._p_fft.setXRange(0.0, float(value), padding=0)
        self._p_rd.setYRange(0.0, float(value), padding=0)

    def _on_auto_level(self) -> None:
        if self._last_rd is None:
            return
        lo, hi = np.percentile(self._last_rd, [5.0, 99.5])
        self._floor_sl.setValue(int(np.clip(lo * 10, 0, 80)))
        self._ceil_sl.setValue(int(np.clip(max(hi, lo + 0.1) * 10, 20, 100)))

    def closeEvent(self, event) -> None:
        self._close_cw_resources()
        self._stop_mode_a()
        event.accept()


def _dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    dark = QColor(30, 30, 46)
    mid = QColor(50, 50, 70)
    light = QColor(220, 220, 220)
    acc = QColor(100, 150, 255)
    pal.setColor(QPalette.Window, dark)
    pal.setColor(QPalette.WindowText, light)
    pal.setColor(QPalette.Base, QColor(20, 20, 35))
    pal.setColor(QPalette.AlternateBase, mid)
    pal.setColor(QPalette.ToolTipBase, mid)
    pal.setColor(QPalette.ToolTipText, light)
    pal.setColor(QPalette.Text, light)
    pal.setColor(QPalette.Button, mid)
    pal.setColor(QPalette.ButtonText, light)
    pal.setColor(QPalette.Highlight, acc)
    pal.setColor(QPalette.HighlightedText, dark)
    app.setPalette(pal)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cal = run_terminal_calibration(args)

    print("\nLaunching closed-loop GUI.")
    print(
        f"Mode A: RF {args.output_freq/1e9:.4f} GHz, {args.num_chirps} chirps, "
        f"{args.sample_rate/1e6:.1f} MSPS, {args.bw/1e6:.0f} MHz BW"
    )
    print(f"Mode B/C: {args.cw_sample_rate/1e3:.0f} kSPS, CW dwell {args.cw_dwell_s:.1f} s")
    print(f"Initial beam: {cal.steer_deg:+.1f} deg, target range: {cal.target_range_m}\n")

    app = QApplication(sys.argv)
    _dark_palette(app)
    win = ClosedLoopWindow(args, cal)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
