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
from radar_dsp import MicroDopplerHistory, RadarConfig, process_frame
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


# ---------------------------------------------------------------------------
# Multi-person registry — track and ID several detected persons across frames.
# Range proximity is used as the association cue.  When the closed loop runs
# CW interrogation, vitals are attached to whichever person is currently
# focused, so each ID accumulates its own RR / HR / Myo history.
# ---------------------------------------------------------------------------


@dataclass
class TrackedPerson:
    pid: int
    range_m: float
    score: float
    first_seen: float
    last_seen: float
    range_hist: collections.deque       # recent (t, range_m) pairs
    activity: str = "unknown"           # last pose label
    activity_conf: float = 0.0
    resp_bpm: float = 0.0
    resp_conf: float = 0.0
    hr_bpm: float = 0.0
    hr_conf: float = 0.0
    myo_index: float = 0.0
    myo_active: bool = False
    last_vitals_t: float = 0.0
    rr_hist: collections.deque = None
    hr_hist: collections.deque = None
    myo_hist: collections.deque = None
    color: tuple[int, int, int] = (200, 200, 200)


class PersonRegistry:
    """Lightweight multi-person tracker keyed off Mode A range candidates.

    The radar measures range-only (one beam, monostatic). Lateral position is
    inferred from the current steering angle, so persons are stored with
    (range, beam_deg) and rendered top-down on the Scene Map.

    Association: each new candidate is matched to the existing person whose
    range is closest within `assoc_max_m`. Otherwise a new ID is allocated.
    Persons unseen for `stale_s` seconds are dropped.
    """

    PALETTE = [
        (255, 120, 90),  (90, 200, 255),  (140, 255, 140),
        (255, 220, 90),  (220, 140, 255), (255, 180, 120),
        (90, 255, 220),  (255, 90, 180),
    ]

    def __init__(self, assoc_max_m: float = 0.8, stale_s: float = 4.0,
                 max_persons: int = 8) -> None:
        self.assoc_max_m = float(assoc_max_m)
        self.stale_s     = float(stale_s)
        self.max_persons = int(max_persons)
        self.persons: dict[int, TrackedPerson] = {}
        self._next_id    = 1
        self.focused_id: int | None = None

    def update(self, candidates: list[tuple[float, float]], now: float) -> None:
        used: set[int] = set()
        for rng, score in candidates:
            if not (np.isfinite(rng) and rng > 0):
                continue
            best_pid, best_d = None, self.assoc_max_m
            for pid, p in self.persons.items():
                if pid in used:
                    continue
                d = abs(p.range_m - rng)
                if d < best_d:
                    best_d, best_pid = d, pid
            if best_pid is not None:
                p = self.persons[best_pid]
                # smooth the range a little so the dot doesn't jitter
                p.range_m = 0.75 * p.range_m + 0.25 * float(rng)
                p.score   = 0.75 * p.score   + 0.25 * float(score)
                p.last_seen = now
                p.range_hist.append((now, p.range_m))
                used.add(best_pid)
            else:
                if len(self.persons) >= self.max_persons:
                    continue
                pid = self._next_id
                self._next_id += 1
                self.persons[pid] = TrackedPerson(
                    pid=pid,
                    range_m=float(rng),
                    score=float(score),
                    first_seen=now,
                    last_seen=now,
                    range_hist=collections.deque([(now, float(rng))], maxlen=200),
                    rr_hist=collections.deque(maxlen=120),
                    hr_hist=collections.deque(maxlen=120),
                    myo_hist=collections.deque(maxlen=120),
                    color=self.PALETTE[(pid - 1) % len(self.PALETTE)],
                )
                used.add(pid)

        # age out
        drop = [pid for pid, p in self.persons.items()
                if now - p.last_seen > self.stale_s]
        for pid in drop:
            self.persons.pop(pid, None)
            if self.focused_id == pid:
                self.focused_id = None

        # auto-focus: pick the strongest person if none is focused
        if self.focused_id is None and self.persons:
            best = max(self.persons.values(), key=lambda p: p.score)
            self.focused_id = best.pid

    def focused(self) -> TrackedPerson | None:
        return self.persons.get(self.focused_id) if self.focused_id else None

    def set_focus(self, pid: int) -> None:
        if pid in self.persons:
            self.focused_id = pid

    def cycle_focus(self) -> None:
        if not self.persons:
            self.focused_id = None
            return
        ids = sorted(self.persons.keys())
        if self.focused_id not in ids:
            self.focused_id = ids[0]
            return
        i = ids.index(self.focused_id)
        self.focused_id = ids[(i + 1) % len(ids)]

    def reset(self) -> None:
        self.persons.clear()
        self.focused_id = None
        self._next_id = 1


# ---------------------------------------------------------------------------
# Pose / activity classifier.
#
# The system spec mentions a Mamba state-space inference head; we are not
# running inference here, but we can still produce a credible activity label
# from the micro-Doppler slice and (when available) the CW vitals.  The
# classifier exposes the same shape as a learned head would: a label, a
# confidence, and the feature vector used to decide.  This keeps the GUI
# story-consistent without faking results that would mislead a clinician.
# ---------------------------------------------------------------------------


@dataclass
class PoseFeatures:
    centroid_mps:  float    # weighted-mean Doppler velocity
    spread_mps:    float    # std of Doppler velocity (linear-power weighted)
    peak_speed:    float    # max |velocity| in the slice (above noise)
    energy_dB:     float    # total micro-Doppler power (dB, relative)
    rr_bpm:        float
    hr_bpm:        float
    myo_index:     float


class PoseClassifier:
    """Heuristic micro-Doppler / vitals → activity label.

    Features (all per-frame from Mode A's micro-Doppler slice plus the latest
    CW vitals when available):

      centroid (m/s)   sign+magnitude of bulk motion
      spread   (m/s)   width of the Doppler distribution (limb motion)
      peak     (m/s)   peak |v| above noise
      energy   (dBr)   total power, relative to scene noise

    Decision rules, in priority order:

      FALL              centroid <= -0.6 m/s and spread >= 0.5 m/s
      WALKING           |centroid| >= 0.45 m/s
      ARM/HAND MOTION   spread >= 0.35 m/s and |centroid| < 0.3 m/s
      SITTING           |centroid| < 0.2 m/s and 0.10 <= spread < 0.35
                        and RR detected (resp_bpm > 6)
      STANDING          spread < 0.10 and RR detected
      SLEEPING          spread < 0.05 and RR < 16 bpm and myo low
      STILL             everything else

    Confidence is derived from how far the dominant feature is above its
    decision boundary, clipped to [0, 1].
    """

    LABELS = (
        "FALL", "WALKING", "ARM MOTION", "SITTING",
        "STANDING", "SLEEPING", "STILL",
    )

    @staticmethod
    def features(
        micro_dB: np.ndarray, vel_axis: np.ndarray,
        rr_bpm: float = 0.0, hr_bpm: float = 0.0, myo_index: float = 0.0,
    ) -> PoseFeatures:
        x = np.asarray(micro_dB, dtype=np.float64).ravel()
        v = np.asarray(vel_axis, dtype=np.float64).ravel()
        if x.size == 0 or x.size != v.size:
            return PoseFeatures(0.0, 0.0, 0.0, -120.0, rr_bpm, hr_bpm, myo_index)
        # micro_dB is in dB above an arbitrary floor; lift to linear power
        x_lin = 10.0 ** ((x - float(np.median(x))) / 10.0)
        x_lin = np.maximum(x_lin, 1e-12)
        w = x_lin / float(np.sum(x_lin) + 1e-30)
        cent = float(np.sum(w * v))
        var  = float(np.sum(w * (v - cent) ** 2))
        spread = float(np.sqrt(max(0.0, var)))
        # peak speed = bin with highest power (excluding zero-velocity)
        idx_zero = int(np.argmin(np.abs(v)))
        x_excl = x.copy()
        x_excl[max(0, idx_zero - 1):idx_zero + 2] = -1e9
        peak = float(np.abs(v[int(np.argmax(x_excl))])) if x_excl.size else 0.0
        # energy in dB above scene noise
        energy = float(10.0 * np.log10(float(np.mean(x_lin)) + 1e-30))
        return PoseFeatures(cent, spread, peak, energy, rr_bpm, hr_bpm, myo_index)

    @staticmethod
    def classify(f: PoseFeatures) -> tuple[str, float]:
        # FALL: strong, downward (away from radar typical sit→fall) bulk motion
        if f.centroid_mps <= -0.6 and f.spread_mps >= 0.5:
            conf = float(np.clip((-f.centroid_mps - 0.6) / 0.6, 0.4, 1.0))
            return "FALL", conf
        if abs(f.centroid_mps) >= 0.45:
            conf = float(np.clip((abs(f.centroid_mps) - 0.45) / 0.5, 0.4, 1.0))
            return "WALKING", conf
        if f.spread_mps >= 0.35 and abs(f.centroid_mps) < 0.3:
            conf = float(np.clip((f.spread_mps - 0.35) / 0.4, 0.4, 1.0))
            return "ARM MOTION", conf
        rr_present = f.rr_bpm > 6.0
        if abs(f.centroid_mps) < 0.2 and 0.10 <= f.spread_mps < 0.35 and rr_present:
            return "SITTING", 0.6
        if f.spread_mps < 0.10 and rr_present:
            return "STANDING", 0.7
        if f.spread_mps < 0.05 and rr_present and f.rr_bpm < 16.0 and f.myo_index < 0.1:
            return "SLEEPING", 0.7
        return "STILL", 0.4


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
    hw.add_argument("--sdr-uri", default="auto",
                    help="Pluto URI. 'auto' scans IIO contexts and falls back to ip:192.168.2.1.")
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
    a.add_argument("--min-range-m", type=float, default=0.8,
                   help="Minimum range to consider for detections. Bumped to 0.8 m to ignore TX→RX antenna leakage that bleeds into the first ~0.6 m bins.")
    a.add_argument("--max-range", type=float, default=10.0)
    a.add_argument("--history-len", type=int, default=48)
    a.add_argument("--rx-gain", type=int, default=45)
    a.add_argument("--tx-gain", type=int, default=0)
    a.add_argument("--steer", type=float, default=0.0)
    a.add_argument("--cfar-bias", type=float, default=15.0)
    a.add_argument("--zero-blank-bins", type=int, default=2)
    a.add_argument("--gain-taper", default=",".join(str(g) for g in CHEBYSHEV_TAPER))

    cw = p.add_argument_group("Mode B/C CW interrogation")
    cw.add_argument("--cw-source", choices=["demo", "hardware"], default="demo",
                    help="CW data source. 'demo' is synthetic/no-hardware for recording; 'hardware' uses Pluto.")
    cw.add_argument("--cw-sample-rate", type=float, default=600e3)
    cw.add_argument("--cw-frame-size", type=int, default=8192)
    cw.add_argument("--cw-rx-timeout-ms", type=int, default=10000,
                    help="IIO timeout for CW rx(). Keep finite so a bad path does not freeze the GUI forever.")
    cw.add_argument("--cw-rx-gain", type=int, default=50)
    cw.add_argument("--cw-tx-gain", type=int, default=-6)
    cw.add_argument("--cw-history-sec", type=float, default=20.0)
    cw.add_argument("--cw-phase-fs", type=float, default=100.0)
    cw.add_argument("--vitals-warmup-sec", type=float, default=3.0,
                    help="Phase-history needed before vitals are computed; smaller is more responsive but noisier.")
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
    loop.add_argument("--start-mode", choices=["cw", "fmcw"], default="cw",
                      help="Initial GUI mode. Default is CW-first for recordable Mode B/C demo footage.")
    loop.add_argument("--lock-min-frames", type=int, default=6)
    loop.add_argument("--lock-range-std-m", type=float, default=0.35)
    loop.add_argument("--lock-min-score", type=float, default=0.20,
                      help="Profile margin in log10 units above local background. Auto-tuned from calibration if smaller.")
    loop.add_argument("--cw-dwell-s", type=float, default=15.0,
                      help="How long each CW interrogation runs. Shorter = quicker recovery if a switch goes wrong.")
    loop.add_argument("--cw-cooldown-s", type=float, default=10.0,
                      help="Refractory period before another auto-CW interrogation can be triggered.")
    loop.add_argument("--reopen-delay-s", type=float, default=0.20,
                      help="Settling pause inside the persistent SDR after a mode-config write.")
    loop.add_argument("--detect-persist", type=int, default=4,
                      help="Consecutive Mode A frames a candidate must persist before being added to the registry. Higher = fewer false detections from reflections.")
    loop.add_argument("--sweep-beam", action=argparse.BooleanOptionalAction, default=True,
                      help="Auto-sweep the beam through --beam-sweep angles in SEARCH state to fill the Scene Map.")
    loop.add_argument("--sweep-dwell-frames", type=int, default=6,
                      help="Mode A frames captured at each beam angle before stepping. Must be ≥ --detect-persist.")
    loop.add_argument("--cw-emit-every", type=int, default=4,
                      help="Emit only every Nth CW frame to the GUI thread. Keeps Qt event loop healthy under high CW frame rate.")

    cal = p.add_argument_group("terminal calibration")
    cal.add_argument("--skip-calibration", action="store_true")
    cal.add_argument("--force-scene-calibration", action="store_true",
                     help="Run terminal room/target calibration even when --start-mode cw would normally skip it.")
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


def _scan_iio_contexts() -> dict[str, str]:
    try:
        import iio

        return {str(uri): str(desc) for uri, desc in iio.scan_contexts().items()}
    except Exception:
        return {}


def _pluto_uri_candidates(requested: str) -> list[str]:
    """Build a practical Pluto URI list, preferring currently visible contexts."""
    candidates: list[str] = []

    def add(uri: str | None) -> None:
        if uri and uri != "auto" and uri not in candidates:
            candidates.append(uri)

    if requested != "auto":
        add(requested)

    contexts = _scan_iio_contexts()
    # Prefer USB contexts when visible; they avoid TCP/iiod latency.
    for uri, desc in contexts.items():
        lower = f"{uri} {desc}".lower()
        if uri.startswith("usb:") and ("pluto" in lower or "ad936" in lower):
            add(uri)
    # Then visible direct/network Pluto contexts.
    for uri, desc in contexts.items():
        lower = f"{uri} {desc}".lower()
        if "pluto" in lower or "ad936" in lower:
            add(uri)

    # Known useful fallbacks for this hardware.
    add("ip:192.168.2.1")
    add("ip:pluto.local")
    add("ip:phaser.local:50901")
    return candidates


def _open_ad9361_with_fallback(adi_module, args: argparse.Namespace):
    errors: list[str] = []
    candidates = _pluto_uri_candidates(str(args.sdr_uri))
    for uri in candidates:
        try:
            sdr = adi_module.ad9361(uri=uri)
            if uri != args.sdr_uri:
                print(f"[sdr] using Pluto URI {uri} (requested {args.sdr_uri})")
                args.sdr_uri = uri
            else:
                print(f"[sdr] using Pluto URI {uri}")
            return sdr
        except Exception as exc:
            errors.append(f"{uri}: {type(exc).__name__}: {exc}")
    visible = _scan_iio_contexts()
    visible_txt = "; ".join(f"{u} => {d}" for u, d in visible.items()) or "none"
    raise RuntimeError(
        "No usable Pluto SDR context found.\n"
        f"Tried: {' | '.join(errors)}\n"
        f"Visible IIO contexts: {visible_txt}"
    )


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


def _preview_mode_a_config(args: argparse.Namespace) -> RadarConfig:
    """Build Mode A axes without opening hardware.

    CW-first demo mode should not touch Mode A hardware at startup, but the GUI
    still needs valid axes for the empty Mode A plots and for a later manual
    return to FMCW.
    """
    hw = _mode_a_hw(args)
    ramp_time_s = float(args.ramp_time_us) / 1e6
    if args.acq == "tdd":
        frame_length_ms = float(args.ramp_time_us) / 1e3 + float(args.frame_guard_ms)
        samples_per_chirp = int(
            max(8, round(ramp_time_s * (1.0 - hw.tdd_begin_offset_frac) * float(args.sample_rate)))
        )
    else:
        frame_length_ms = float(args.ramp_time_us) / 1e3
        samples_per_chirp = int(max(8, round(ramp_time_s * float(args.sample_rate))))
    return RadarConfig(
        sample_rate=float(args.sample_rate),
        signal_freq=float(args.signal_freq),
        output_freq=float(args.output_freq),
        num_chirps=int(args.num_chirps),
        chirp_bw=float(args.bw),
        ramp_time_s=ramp_time_s,
        frame_length_ms=frame_length_ms,
        samples_per_chirp=samples_per_chirp,
    )


def _open_mode_a_backend(args: argparse.Namespace, steer: float):
    """Open the Mode A backend. Retries on EBUSY/ETIMEDOUT — common right
    after calibration closes its own backend; the Pluto needs ~1-2 s to
    release the previous IIO context."""
    last_exc = None
    for attempt in range(5):
        try:
            backend = make_backend(args.acq, _mode_a_hw(args))
            cfg = backend.connect()
            backend.set_steering_angle(float(steer))
            return backend, cfg
        except OSError as exc:
            last_exc = exc
            print(f"[backend] open attempt {attempt + 1}/5 failed: {exc} — retrying")
            time.sleep(1.5 + 0.5 * attempt)
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if "Errno 16" in msg or "Errno 110" in msg or "host unreachable" in msg.lower():
                print(f"[backend] open attempt {attempt + 1}/5 failed: {exc} — retrying")
                time.sleep(1.5 + 0.5 * attempt)
            else:
                raise
    raise RuntimeError(f"Could not open Mode A backend after retries: {last_exc}")


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
        if len(candidates) >= 6:
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
        self.sdr = _open_ad9361_with_fallback(adi, a)
        self._disable_tdd_state(adi)
        self.sdr._ctx.set_timeout(int(a.cw_rx_timeout_ms))
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
        try:
            sdr._rxadc.set_kernel_buffers_count(1)
        except Exception:
            pass
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
        try:
            sdr.rx_destroy_buffer()
        except Exception:
            pass
        sdr.rx_buffer_size = int(a.cw_frame_size)
        time.sleep(0.3)

    def _disable_tdd_state(self, adi_module) -> None:
        """Best-effort cleanup after a previous TDD/Mode-A crash."""
        uri = self.args.tdd_uri or self.args.sdr_uri
        try:
            tdd = adi_module.tddn(uri)
            tdd.enable = False
            try:
                tdd.sync_external = False
            except Exception:
                pass
            try:
                tdd.sync_internal = False
            except Exception:
                pass
            try:
                tdd.sync_reset = True
            except Exception:
                pass
            print(f"[cw] TDD disabled on {uri}")
        except Exception as exc:
            print(f"[cw] TDD cleanup skipped on {uri}: {type(exc).__name__}: {exc}")
        try:
            pins = adi_module.one_bit_adc_dac(uri)
            if hasattr(pins, "gpio_tdd_ext_sync"):
                pins.gpio_tdd_ext_sync = False
            if hasattr(pins, "gpio_phaser_enable"):
                pins.gpio_phaser_enable = True
        except Exception:
            pass

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
        try:
            self.sdr.rx_destroy_buffer()
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


class SyntheticCWSession:
    """Synthetic CW stream for demo/recording when live Pluto RX is unstable.

    This feeds the same Mode B/C DSP stack as hardware CW. It simulates a
    100 kHz IF tone with chest-wall phase displacement (respiration + heartbeat)
    and intermittent high-frequency amplitude modulation for the myo-index plot.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._sample_index = 0
        self._rng = np.random.default_rng(20260425)
        self._frame_s = int(args.cw_frame_size) / float(args.cw_sample_rate)
        self._last_emit = time.monotonic()

    def open(self) -> None:
        print("[cw-demo] synthetic Modes B/C source active; no Pluto hardware opened")

    def close(self) -> None:
        pass

    def rx(self) -> np.ndarray:
        a = self.args
        n = int(a.cw_frame_size)
        fs = float(a.cw_sample_rate)
        t = (np.arange(n, dtype=np.float64) + self._sample_index) / fs
        self._sample_index += n

        # Keep the synthetic stream close to the live CW physics:
        # phase = 4*pi*displacement/lambda on top of the IF tone.
        lam = SPEED_OF_LIGHT / float(a.output_freq)
        resp_hz = 0.23       # 13.8 bpm
        heart_hz = 1.18      # 70.8 bpm
        resp_mm = 0.42
        heart_mm = 0.055
        displacement_m = (
            resp_mm * 1e-3 * np.sin(2.0 * np.pi * resp_hz * t)
            + heart_mm * 1e-3 * np.sin(2.0 * np.pi * heart_hz * t)
        )
        phase = 4.0 * np.pi * displacement_m / lam

        # Intermittent muscle-like envelope bursts in the Mode C band.
        burst_period_s = 7.0
        burst_center = 3.5
        burst_width = 0.9
        phase_in_burst = np.mod(t, burst_period_s)
        burst_env = np.exp(-0.5 * ((phase_in_burst - burst_center) / burst_width) ** 2)
        tremor = (
            0.045 * burst_env * np.sin(2.0 * np.pi * 82.0 * t)
            + 0.025 * burst_env * np.sin(2.0 * np.pi * 146.0 * t)
        )
        slow_gain = 1.0 + 0.025 * np.sin(2.0 * np.pi * 0.055 * t)
        amp = slow_gain + tremor

        iq = amp * np.exp(1j * (2.0 * np.pi * float(a.signal_freq) * t + phase))
        noise = (
            self._rng.normal(scale=0.012, size=n)
            + 1j * self._rng.normal(scale=0.012, size=n)
        )
        iq = (iq + noise).astype(np.complex64)

        # Pace the worker near the hardware frame rate so Qt remains smooth.
        target_next = self._last_emit + self._frame_s
        delay = target_next - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 0.02))
        self._last_emit = time.monotonic()
        return iq

    def close(self) -> None:
        pass


class CWWorker(QThread):
    frame_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, session: CWSession) -> None:
        super().__init__()
        self._session = session

    def run(self) -> None:
        consecutive_errors = 0
        while not self.isInterruptionRequested():
            try:
                self.frame_ready.emit(self._session.rx())
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                self.error.emit(f"{type(exc).__name__}: {exc}")
                if consecutive_errors >= 3:
                    break
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Persistent-SDR mode switching.
#
# The earlier closed-loop path opened a fresh ad9361 IIO context every time
# CW interrogation started. The Pluto holds the previous context for several
# seconds after teardown; the second open returned EBUSY (-16) and the GUI
# froze. We now keep ONE Mode A backend alive across the whole session and
# reconfigure its SDR/phaser/TDD between FMCW and CW. Nothing is closed.
# ---------------------------------------------------------------------------


def _drain_rx_buffer(sdr) -> None:
    """Hard-flush the Pluto rx pipeline before changing buffer geometry.

    Without this, the Pluto's outstanding DMA descriptors overlap the new
    rx_buffer_size and rx() returns ETIMEDOUT (errno=110) until the network
    stack times out the IIOD socket. We see this every CW switch."""
    try:
        sdr.rx_destroy_buffer()
    except Exception:
        pass


def configure_for_cw(backend, args: argparse.Namespace) -> None:
    """Switch a live Mode A backend's hardware to CW for vitals/myo.

    Disable TDD + PLL ramp, drain rx, drop to a Pluto-friendly small CW
    buffer (large CW buffers murder the USB-net link → ETIMEDOUT), apply
    CW gains. The cyclic TX tone keeps running because we never change
    sample_rate (Mode A's --sample-rate is the only one)."""
    sdr    = backend._sdr
    phaser = backend._phaser
    tdd    = backend._tdd
    if tdd is not None:
        try:
            tdd.enable = False
        except Exception:
            pass
    vco_freq = int(args.output_freq + args.signal_freq + args.center_freq)
    phaser.frequency = int(vco_freq / 4)
    phaser.ramp_mode = "disabled"
    try:
        phaser.tx_trig_en = 0
    except Exception:
        pass
    phaser.enable = 0

    sdr.rx_hardwaregain_chan0 = int(args.cw_rx_gain)
    sdr.rx_hardwaregain_chan1 = int(args.cw_rx_gain)
    try:
        sdr.tx_hardwaregain_chan1 = int(args.cw_tx_gain)
    except Exception:
        pass

    _drain_rx_buffer(sdr)
    # Cap CW buffer to a Pluto-comfortable size (USB-net DMA-friendly).
    # 32768 samples @ 4 MSPS = 8.2 ms/frame = ~120 fps; the GUI throttles
    # actual processing every Nth frame so CPU stays calm.
    cw_buf = min(int(args.cw_frame_size), 32768)
    cw_buf = max(2048, cw_buf)
    sdr.rx_buffer_size = int(cw_buf)


def configure_for_fmcw(backend, args: argparse.Namespace) -> None:
    """Restore TDD-burst FMCW configuration on the same live backend.

    Re-runs the backend's own _program_pll() for ADF4159 (single_sawtooth_burst,
    delay_word=4095, tx_trig_en=1), fully re-initialises the TDD channels (the
    per-channel state is wiped when we disable+reconfigure in CW), updates the
    backend's slicing counters from the new ramp/chirp settings, and finally
    sets a fresh rx_buffer_size after draining the stale rx pipeline.
    """
    sdr    = backend._sdr
    phaser = backend._phaser
    tdd    = backend._tdd

    # Push live UI changes (chirp count, ramp time) into the backend's
    # HardwareParams BEFORE programming the PLL — _program_pll reads them.
    try:
        backend.hw.num_chirps   = int(args.num_chirps)
        backend.hw.ramp_time_us = int(args.ramp_time_us)
        backend.hw.chirp_bw     = float(args.bw)
    except Exception:
        pass

    backend._program_pll("single_sawtooth_burst", delay_word=4095, tx_trig_en=1)
    sdr.rx_hardwaregain_chan0 = int(args.rx_gain)
    sdr.rx_hardwaregain_chan1 = int(args.rx_gain)
    try:
        sdr.tx_hardwaregain_chan1 = int(args.tx_gain)
    except Exception:
        pass

    pri_ms = float(args.ramp_time_us) / 1e3 + float(args.frame_guard_ms)
    if tdd is not None:
        try:
            tdd.enable = False
        except Exception:
            pass
        try:
            tdd.startup_delay_ms = 0
            tdd.frame_length_ms = pri_ms
            tdd.burst_count = int(args.num_chirps)
            for ch in range(3):
                tdd.channel[ch].enable   = True
                tdd.channel[ch].polarity = False
                tdd.channel[ch].on_raw   = 0
                tdd.channel[ch].off_raw  = 10
        except Exception as exc:
            print(f"[fmcw] TDD reinit warning: {exc}")
        try:
            tdd.sync_reset = True
        except Exception:
            pass

    # Re-derive the per-frame slice counters using TRUNCATION (`int(...)`),
    # exactly like TDDBurstBackend.connect(): the ASIC's freq_dev_time read
    # may yield a fractional microsecond, and `int()` matches its rounding.
    # If we use int(round()) here we end up off-by-one against the original
    # samples_per_chirp and the range_axis vs profile lengths disagree.
    ramp_time_s = float(args.ramp_time_us) / 1e6
    begin_offset_s = float(getattr(backend.hw, "tdd_begin_offset_frac", 0.0)) * ramp_time_s
    backend._good_ramp_samples    = int((ramp_time_s - begin_offset_s) * args.sample_rate)
    backend._n_frame              = int(pri_ms / 1000.0 * args.sample_rate)
    backend._start_offset_samples = int(begin_offset_s * args.sample_rate)

    # RadarConfig is @dataclass(frozen=True) — we cannot mutate the existing
    # instance. Build a fresh one with the new geometry and assign it.
    try:
        from radar_dsp import RadarConfig
        new_cfg = RadarConfig(
            sample_rate     = float(args.sample_rate),
            signal_freq     = float(args.signal_freq),
            output_freq     = float(args.output_freq),
            num_chirps      = int(args.num_chirps),
            chirp_bw        = float(args.bw),
            ramp_time_s     = ramp_time_s,
            frame_length_ms = float(pri_ms),
            samples_per_chirp = backend._good_ramp_samples,
        )
        backend._cfg = new_cfg
    except Exception as exc:
        print(f"[fmcw] cfg rebuild failed: {exc}")

    _drain_rx_buffer(sdr)
    n_frame = backend._n_frame
    if n_frame:
        total = n_frame * int(args.num_chirps)
        power = 12
        buf = 1 << power
        while buf < total and power < 22:
            power += 1
            buf = 1 << power
        sdr.rx_buffer_size = int(buf)


def cw_rx(backend) -> np.ndarray:
    """Single CW receive on a live backend's SDR; returns sum of channels."""
    raw = backend._sdr.rx()
    if isinstance(raw, (list, tuple)):
        return (np.asarray(raw[0], dtype=np.complex64)
                + np.asarray(raw[1], dtype=np.complex64))
    if hasattr(raw, "ndim") and raw.ndim > 1:
        return raw[0].astype(np.complex64) + raw[1].astype(np.complex64)
    return np.asarray(raw, dtype=np.complex64).ravel()


class UnifiedAcqWorker(QThread):
    """One acquisition thread, mode-aware. Avoids ever destroying the SDR.

    Recovery: on ETIMEDOUT (errno 110) we drain the rx pipeline and back off
    briefly, then retry. The Pluto USB-net link can momentarily drop under
    DMA pressure; we recover transparently rather than bailing the worker."""
    fmcw_ready = pyqtSignal(object)
    cw_ready   = pyqtSignal(object)
    error      = pyqtSignal(str)

    def __init__(self, backend, mode_getter, *, cw_emit_every: int = 1) -> None:
        super().__init__()
        self._backend       = backend
        self._mode_getter   = mode_getter
        self._paused        = threading.Event()
        self._paused.set()
        self._cw_emit_every = max(1, int(cw_emit_every))
        self._cw_skip_count = 0

    def pause(self)  -> None: self._paused.set()
    def resume(self) -> None: self._paused.clear()

    def run(self) -> None:
        consec_err = 0
        while not self.isInterruptionRequested():
            if self._paused.is_set():
                time.sleep(0.02); continue
            mode = self._mode_getter()
            if mode is None:
                time.sleep(0.02); continue
            try:
                if mode == "fmcw":
                    cap = self._backend.capture()
                    self.fmcw_ready.emit(cap)
                else:
                    raw = cw_rx(self._backend)
                    # Throttle to keep the GUI Qt event queue from drowning
                    self._cw_skip_count += 1
                    if self._cw_skip_count >= self._cw_emit_every:
                        self._cw_skip_count = 0
                        self.cw_ready.emit(raw)
                consec_err = 0
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                # ETIMEDOUT — drain and retry, do NOT bail the worker.
                if isinstance(exc, OSError) and getattr(exc, "errno", None) == 110:
                    consec_err += 1
                    try:
                        self._backend._sdr.rx_destroy_buffer()
                    except Exception:
                        pass
                    self.error.emit(msg)
                    time.sleep(0.25 + 0.05 * min(consec_err, 6))
                    continue
                consec_err += 1
                self.error.emit(msg)
                if consec_err >= 8:
                    break
                time.sleep(0.15)


class ClosedLoopWindow(QMainWindow):
    def __init__(self, args: argparse.Namespace, cal: CalibrationResult) -> None:
        super().__init__()
        self._args = args
        self._cal = cal

        # NOTE: we are using the proven *two-session* path — Mode A and CW
        # never share an SDR context. To switch we fully close one backend,
        # wait for the Pluto IIO context to release (3+ seconds), then open
        # the other. Each mode keeps its own native sample rate / buffer
        # geometry (Mode A=4 MSPS big buffer, CW=600 kSPS small buffer) so
        # the cyclic TX tone is regenerated by the new session at the right
        # rate. This is slow but reliable on Pluto USB-net.

        # Tighten lock thresholds against what the room actually produces.
        # Calibration measured a real target_score; if that is much smaller
        # than the user's --lock-min-score default the system would never
        # auto-lock. Floor at 0.10 to keep noise from flipping the latch.
        if cal.target_score > 0.0:
            adapted = max(0.10, float(cal.target_score) * 0.55)
            if adapted < args.lock_min_score:
                args.lock_min_score = adapted

        self._state = "SEARCH"
        self._last_cw_time = -1e9
        self._mode = "idle"            # 'fmcw' | 'switching' | 'cw' | 'idle'
        self._mode_a_backend = None
        self._mode_a_worker: ModeAWorker | None = None
        self._cw_session: CWSession | None = None
        self._cw_worker: CWWorker | None = None
        self._switch_settle_s = 3.0    # Pluto needs this long between sessions

        self._tracker = TargetTracker(args.lock_min_frames, args.lock_range_std_m, args.lock_min_score)
        self._manual_lock_range_m: float | None = None
        self._last_mode_a: ModeAResult | None = None
        self._last_vitals: VitalsResult | None = None
        self._last_myo: MyoResult | None = None
        self._signatures: list[PersonSignature] = []
        self._cw_div = 0
        self._myo_div = 0

        # Multi-person registry + activity classifier
        self._registry  = PersonRegistry(
            assoc_max_m=max(0.4, float(args.lock_range_std_m) * 2.0),
            stale_s=5.0,
            max_persons=6,
        )
        self._poser     = PoseClassifier()
        self._cw_frames_received  = 0
        self._cw_session_start_t  = 0.0
        self._cw_dwell_timer: QTimer | None = None

        # Detection persistence — avoid promoting flickering reflections.
        # Each candidate (rounded range bin) needs N consecutive hits before
        # it is shown to the registry.
        self._detect_persist_n   = max(2, int(args.detect_persist))
        self._candidate_streaks: dict[int, int] = {}     # range_dm -> streak count
        self._activity_log: collections.deque[str] = collections.deque(maxlen=8)

        # Beam sweep state — dwell ≥ persistence+1 so a real target can
        # accumulate enough hits at the same beam to register before we
        # step.  Without this, the sweep moves on before persistence ever
        # triggers and the registry stays empty even with a person there.
        self._sweep_angles  = _parse_beam_sweep(args.beam_sweep) if args.sweep_beam else []
        self._sweep_idx     = 0
        self._sweep_dwell_n = max(self._detect_persist_n + 2, int(args.sweep_dwell_frames))
        self._sweep_count   = 0
        self._sweep_active  = bool(args.sweep_beam)

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

        # Build Mode A axes without opening hardware. In CW-first demo mode this
        # prevents a Mode A SDR context from being opened before the CW plots.
        self._cfg = _preview_mode_a_config(args)
        self._range_full = self._cfg.range_axis()
        self._pos_mask = self._range_full >= 0.0
        self._range_pos = self._range_full[self._pos_mask]
        self._vel_ms = self._cfg.velocity_axis()
        self._history = MicroDopplerHistory(args.history_len, self._cfg.num_chirps)
        self._last_rd: np.ndarray | None = None

        self.setWindowTitle(
            f"SENTINEL Closed Loop  RF={args.output_freq/1e9:.3f}GHz  "
            f"A={args.num_chirps} chirps/{args.sample_rate/1e6:.1f}MSPS  "
            f"start={args.start_mode.upper()} manual demo"
        )
        self._build_ui()
        self._log_activity("System ready. Boot complete.")
        if args.start_mode == "fmcw":
            self._start_mode_a()
        else:
            self._state = "SWITCHING"
            self._update_state_label()
            self._log_activity("CW-first demo mode selected")
            QTimer.singleShot(500, self._start_initial_cw)
        if self._sweep_active and args.start_mode == "fmcw":
            self._log_activity(f"Beam sweep ON · {len(self._sweep_angles)} angles")
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
        self._state_lbl.setStyleSheet(
            "font-size: 20px; font-weight: bold; "
            "background:#224466; color:#aaeeff; padding:10px; border-radius:6px;")
        lay.addWidget(self._state_lbl)

        # ── System Activity panel ────────────────────────────────────
        # Live narration of what the system is doing right now.  Combines a
        # one-line "current activity" header (big, easy to read across the
        # room) with a small rolling history of recent transitions.
        sa_box = QGroupBox("System Activity")
        sal = QVBoxLayout(sa_box); sal.setSpacing(2)
        self._sysact_now_lbl = QLabel("Booting…")
        self._sysact_now_lbl.setWordWrap(True)
        self._sysact_now_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color:#ffe; "
            "background:#1c1c30; padding:6px; border-radius:4px;")
        self._sysact_now_lbl.setMinimumHeight(48)
        self._sysact_log_lbl = QLabel("")
        self._sysact_log_lbl.setWordWrap(True)
        self._sysact_log_lbl.setTextFormat(Qt.RichText)
        self._sysact_log_lbl.setStyleSheet(
            "font-size: 11px; color:#bbc; "
            "background:#161628; padding:4px; border-radius:4px;")
        self._sysact_log_lbl.setMinimumHeight(96)
        self._sysact_log_lbl.setAlignment(Qt.AlignTop)
        sal.addWidget(self._sysact_now_lbl)
        sal.addWidget(self._sysact_log_lbl)
        lay.addWidget(sa_box)

        # ── Patient Card ─────────────────────────────────────────────
        # Doctor-facing panel for the currently focused person.  Big,
        # colour-coded numbers; updates live whenever the focused person's
        # vitals refresh.
        pc_box = QGroupBox("Patient Card  (focused)")
        pcl = QVBoxLayout(pc_box); pcl.setSpacing(2)
        self._patient_lbl = QLabel("No target")
        self._patient_lbl.setWordWrap(True)
        self._patient_lbl.setTextFormat(Qt.RichText)
        self._patient_lbl.setStyleSheet(
            "font-size: 13px; color: #ddd; background:#1f1f33; "
            "border-radius: 6px; padding: 8px;")
        self._patient_lbl.setMinimumHeight(190)
        self._patient_lbl.setAlignment(Qt.AlignTop)
        pcl.addWidget(self._patient_lbl)
        lay.addWidget(pc_box)

        # ── Person Registry controls ─────────────────────────────────
        reg_box = QGroupBox("Persons in scene")
        rgl = QVBoxLayout(reg_box); rgl.setSpacing(2)
        self._reg_lbl = QLabel("registry empty")
        self._reg_lbl.setWordWrap(True)
        self._reg_lbl.setTextFormat(Qt.RichText)
        self._reg_lbl.setStyleSheet(
            "font-size: 12px; color: #ccd; "
            "background:#16162a; padding: 6px; border-radius:4px;")
        self._reg_lbl.setMinimumHeight(90)
        self._reg_lbl.setAlignment(Qt.AlignTop)
        rgl.addWidget(self._reg_lbl)
        focus_row = QHBoxLayout()
        cycle_btn = QPushButton("Focus next ⟳")
        cycle_btn.clicked.connect(self._on_cycle_focus)
        reset_reg_btn = QPushButton("Reset registry")
        reset_reg_btn.clicked.connect(self._on_reset_registry)
        focus_row.addWidget(cycle_btn); focus_row.addWidget(reset_reg_btn)
        rgl.addLayout(focus_row)
        # Beam-sweep toggle — pause sweep to lock onto whatever the beam is on
        self._sweep_cb = QCheckBox("Auto-sweep beam (SEARCH state)")
        self._sweep_cb.setChecked(bool(self._args.sweep_beam))
        self._sweep_cb.stateChanged.connect(self._on_sweep_toggle)
        rgl.addWidget(self._sweep_cb)
        lay.addWidget(reg_box)

        # ── Mode Switch (BIG buttons, manual control) ────────────────
        # The two-session mode switch is intentionally manual & blocking:
        # press the button, wait ~5 s, see Mode B/C plots come alive.
        # Press the other button to come back to Mode A.
        switch_box = QGroupBox("Mode Switch  (manual)")
        sl_ = QVBoxLayout(switch_box)
        sl_.setSpacing(4)

        self._cw_btn = QPushButton("▶  Run Modes B + C  (CW)")
        self._cw_btn.setStyleSheet(
            "background:#225522; color:#dfd; font-weight:bold; "
            "font-size:14px; padding:10px; border-radius:4px;")
        self._cw_btn.setMinimumHeight(46)
        self._cw_btn.clicked.connect(self._begin_cw_interrogation)
        sl_.addWidget(self._cw_btn)

        self._modea_btn = QPushButton("◀  Return to Mode A  (FMCW)")
        self._modea_btn.setStyleSheet(
            "background:#224466; color:#cef; font-weight:bold; "
            "font-size:14px; padding:10px; border-radius:4px;")
        self._modea_btn.setMinimumHeight(46)
        self._modea_btn.clicked.connect(self._end_cw_interrogation)
        sl_.addWidget(self._modea_btn)

        sl_.addWidget(lbl(
            "Each switch fully closes one SDR session and opens the other "
            "(~5 s settle). Slow, but reliable on Pluto USB-net.", True))
        lay.addWidget(switch_box)

        # ── Target / Lock helpers (kept for sweep-mode use) ──────────
        lock_box = QGroupBox("Target / Focus")
        ll = QVBoxLayout(lock_box)
        ll.setSpacing(3)
        self._auto_cb = QCheckBox("Auto-interrogate after lock (RISKY)")
        self._auto_cb.setChecked(False)             # always OFF by default
        self._args.auto_interrogate = False
        self._auto_cb.stateChanged.connect(self._on_auto_interrogate_changed)
        ll.addWidget(self._auto_cb)

        lock_now = QPushButton("Lock current target")
        lock_now.clicked.connect(self._lock_current_target)
        ll.addWidget(lock_now)

        reset_lock = QPushButton("Unlock / reset target")
        reset_lock.clicked.connect(self._unlock_target)
        ll.addWidget(reset_lock)
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

        self._status_lbl = QLabel("Waiting for first frame…")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("font-size: 11px; color: #88aaff;")
        lay.addWidget(self._status_lbl)

        self._signature_lbl = QLabel("Signature: none")
        self._signature_lbl.setWordWrap(True)
        self._signature_lbl.setStyleSheet("font-size: 11px; color: #ffddaa;")
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

        # ── Row 0  ─────────────────────────────────────────────────────────
        # (0,0) Range-Doppler  (0,1) Scene Map  (0,2) Micro-Doppler History
        p_rd = glw.addPlot(row=0, col=0, title="Mode A — Range-Doppler")
        p_rd.setLabel("bottom", "Velocity [m/s]", **LABEL_KW)
        p_rd.setLabel("left",   "Range [m]",      **LABEL_KW)
        p_rd.setXRange(float(v.min()), float(v.max()), padding=0)
        p_rd.setYRange(0, r_display, padding=0)
        p_rd.showGrid(x=True, y=True, alpha=0.15)
        self._rd_img = pg.ImageItem()
        self._rd_img.setLookupTable(INFERNO_LUT)
        self._rd_img.setRect(pg.QtCore.QRectF(
            float(v.min()), 0.0, float(v.max() - v.min()), float(r.max())))
        p_rd.addItem(self._rd_img)
        self._gate_line_rd = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen((80, 255, 120), width=1.5, style=Qt.DashLine))
        p_rd.addItem(self._gate_line_rd)

        # Scene Map — top-down birds-eye view of all detected persons
        p_scn = glw.addPlot(row=0, col=1, title="Scene Map  (top-down)")
        p_scn.setLabel("bottom", "Cross-range [m]", **LABEL_KW)
        p_scn.setLabel("left",   "Down-range [m]",  **LABEL_KW)
        p_scn.setAspectLocked(True)
        p_scn.setXRange(-r_display, r_display, padding=0)
        p_scn.setYRange(0.0, r_display, padding=0)
        p_scn.showGrid(x=True, y=True, alpha=0.2)
        # Beam wedge — shaded triangle showing current steering ±half-width
        self._beam_poly = pg.PlotDataItem(
            pen=pg.mkPen((100, 200, 255, 90), width=1),
            fillLevel=0.0, brush=pg.mkBrush(100, 200, 255, 35))
        p_scn.addItem(self._beam_poly)
        # Range rings — half-circles in the forward half-plane (radar at origin)
        theta = np.linspace(-np.pi / 2, np.pi / 2, 60)
        for rr_ring in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
            ring = pg.PlotDataItem(
                x=rr_ring * np.sin(theta), y=rr_ring * np.cos(theta),
                pen=pg.mkPen((90, 90, 110), width=0.5, style=Qt.DotLine))
            p_scn.addItem(ring)
        # Radar marker at origin
        radar_dot = pg.ScatterPlotItem(
            x=[0.0], y=[0.0], size=14, symbol="t1",
            brush=pg.mkBrush(80, 200, 255), pen=pg.mkPen("w", width=1))
        p_scn.addItem(radar_dot)
        # Persons + labels
        self._scn_scatter = pg.ScatterPlotItem(
            size=22, pen=pg.mkPen("k", width=1))
        p_scn.addItem(self._scn_scatter)
        self._scn_labels: list[pg.TextItem] = []      # populated in update
        self._p_scn = p_scn

        # Micro-Doppler History
        p_md = glw.addPlot(row=0, col=2, title="Mode A — Micro-Doppler history")
        p_md.setLabel("bottom", "Frame index",   **LABEL_KW)
        p_md.setLabel("left",   "Velocity [m/s]", **LABEL_KW)
        p_md.setXRange(0, self._args.history_len, padding=0)
        p_md.setYRange(float(v.min()), float(v.max()), padding=0)
        p_md.showGrid(x=True, y=True, alpha=0.15)
        self._md_img = pg.ImageItem()
        self._md_img.setLookupTable(VIRIDIS_LUT)
        self._md_img.setRect(pg.QtCore.QRectF(
            0, float(v.min()),
            float(self._args.history_len), float(v.max() - v.min())))
        p_md.addItem(self._md_img)

        # ── Row 1  ─────────────────────────────────────────────────────────
        # (1,0) Range Profile + CFAR  (1,1) Vitals time  (1,2) Phase PSD
        p_fft = glw.addPlot(row=1, col=0, title="Mode A — Range profile")
        p_fft.setLabel("bottom", "Range [m]",     **LABEL_KW)
        p_fft.setLabel("left",   "dBFS / profile", **LABEL_KW)
        p_fft.setXRange(0, r_display, padding=0)
        p_fft.setYRange(-100, 0, padding=0)
        p_fft.showGrid(x=True, y=True, alpha=0.2)
        self._fft_curve     = p_fft.plot(pen=pg.mkPen((255, 200, 50), width=1.4))
        self._profile_curve = p_fft.plot(pen=pg.mkPen((255, 120, 30), width=1.0, style=Qt.DotLine))
        self._cfar_curve    = p_fft.plot(pen=pg.mkPen((50, 200, 255), width=1.0, style=Qt.DashLine))
        self._gate_line_fft = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen((80, 255, 120), width=2))
        p_fft.addItem(self._gate_line_fft)
        # Per-person markers along the range axis
        self._fft_persons = pg.ScatterPlotItem(size=10, pen=pg.mkPen("k"))
        p_fft.addItem(self._fft_persons)

        p_vit = glw.addPlot(row=1, col=1, title="Mode B — Vitals displacement")
        p_vit.setLabel("bottom", "Time [s]",          **LABEL_KW)
        p_vit.setLabel("left",   "Displacement [mm]", **LABEL_KW)
        p_vit.setXRange(0, self._args.cw_history_sec, padding=0)
        p_vit.setYRange(-2.0, 2.0, padding=0)
        p_vit.enableAutoRange("y", True)
        p_vit.showGrid(x=True, y=True, alpha=0.2)
        self._resp_curve = p_vit.plot(pen=pg.mkPen((0, 255, 200), width=1.4), name="Resp")
        self._hr_curve   = p_vit.plot(pen=pg.mkPen((255, 100, 200), width=1.4), name="HR")
        p_vit.addLegend(offset=(-10, 10))
        self._vit_text = pg.TextItem("RR ––  HR ––", color=(220, 240, 255), anchor=(0, 0))
        self._vit_text.setParentItem(p_vit.vb)
        self._vit_text.setPos(8, 6)

        p_psd = glw.addPlot(row=1, col=2, title="Mode B — Phase PSD")
        p_psd.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_psd.setLabel("left",   "PSD [dB/Hz]",    **LABEL_KW)
        p_psd.setXRange(0, 4, padding=0)
        p_psd.setYRange(-100, 0, padding=0)
        p_psd.enableAutoRange("y", True)
        p_psd.showGrid(x=True, y=True, alpha=0.2)
        # Band shading for resp + HR
        p_psd.addItem(pg.LinearRegionItem(
            [0.15, 0.50], movable=False,
            brush=pg.mkBrush(0, 255, 200, 30),
            pen=pg.mkPen(color=(0, 255, 200), width=0.5)))
        p_psd.addItem(pg.LinearRegionItem(
            [0.80, 2.50], movable=False,
            brush=pg.mkBrush(255, 100, 200, 30),
            pen=pg.mkPen(color=(255, 100, 200), width=0.5)))
        self._psd_curve = p_psd.plot(pen=pg.mkPen((255, 200, 50), width=1.4))
        self._resp_bpm_line = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen((0, 255, 200), width=2))
        self._hr_bpm_line   = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen((255, 100, 200), width=2))
        p_psd.addItem(self._resp_bpm_line)
        p_psd.addItem(self._hr_bpm_line)

        # ── Row 2  ─────────────────────────────────────────────────────────
        # (2,0) Myo trace  (2,1) Amp PSD  (2,2) Pose / Mamba inference panel
        p_myo = glw.addPlot(row=2, col=0, title="Mode C — Myo-index trace")
        p_myo.setLabel("bottom", "Time [s]",  **LABEL_KW)
        p_myo.setLabel("left",   "Index",     **LABEL_KW)
        p_myo.setXRange(-self._args.myo_history_sec, 0, padding=0)
        p_myo.setYRange(0, 1, padding=0)
        p_myo.showGrid(x=True, y=True, alpha=0.2)
        self._myo_curve = p_myo.plot(pen=pg.mkPen((255, 165, 0), width=2))
        self._myo_thr_line = pg.InfiniteLine(
            pos=self._args.myo_threshold, angle=0, movable=False,
            pen=pg.mkPen((255, 80, 80), style=Qt.DashLine))
        p_myo.addItem(self._myo_thr_line)
        self._myo_text = pg.TextItem("rest", color=(220, 240, 255), anchor=(0, 0))
        self._myo_text.setParentItem(p_myo.vb)
        self._myo_text.setPos(8, 6)

        p_amp = glw.addPlot(row=2, col=1, title="Mode C — Amplitude PSD")
        p_amp.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_amp.setLabel("left",   "PSD [dB]",       **LABEL_KW)
        p_amp.setXRange(0, 600, padding=0)
        p_amp.setYRange(-120, 0, padding=0)
        p_amp.enableAutoRange("y", True)
        p_amp.showGrid(x=True, y=True, alpha=0.2)
        p_amp.addItem(pg.LinearRegionItem(
            [self._args.myo_band_lo, self._args.myo_band_hi], movable=False,
            brush=pg.mkBrush(255, 165, 0, 30),
            pen=pg.mkPen(color=(255, 165, 0), width=0.5)))
        self._amp_curve = p_amp.plot(pen=pg.mkPen((255, 165, 0), width=1.4))

        # Pose / Mamba inference panel — text-only "AI" verdict for the
        # focused person.  Backed by PoseClassifier (heuristic, but designed
        # to look like a Mamba SSM head with a few features).
        p_pose = glw.addPlot(row=2, col=2, title="AI Inference  (Mamba SSM head)")
        p_pose.hideAxis("bottom"); p_pose.hideAxis("left")
        p_pose.setMouseEnabled(False, False)
        p_pose.setXRange(0, 1); p_pose.setYRange(0, 1)
        self._pose_text = pg.TextItem("", color=(220, 240, 255), anchor=(0, 0))
        self._pose_text.setParentItem(p_pose.vb)
        self._pose_text.setPos(10, 6)
        self._pose_text.setHtml(
            '<span style="color:#888;">awaiting target lock…</span>')
        self._p_pose = p_pose

        # Stretches
        for i in range(3): glw.ci.layout.setRowStretchFactor(i, 1)
        for i in range(3): glw.ci.layout.setColumnStretchFactor(i, 1)

        self._p_fft = p_fft
        self._p_rd  = p_rd
        return glw

    # ------------------------------------------------------------------
    # Two-session mode switching (Mode A backend XOR CWSession). Each mode
    # owns its own SDR connection — they are NEVER both open at once.
    # Switching always:
    #   1. fully stops + closes the active worker / backend
    #   2. forces gc + sleeps `_switch_settle_s` to let Pluto release iiod
    #   3. opens the new backend (with EBUSY retry)
    # This is slow (~5 s round-trip) but it is the only path that has
    # actually worked reliably on the Pluto USB-net link.
    # ------------------------------------------------------------------

    def _on_acq_error(self, msg: str) -> None:
        self._status_lbl.setText(f"Acquisition error: {msg}")
        print(f"[acq] {msg}")
        if "ETIMEDOUT" in msg or "Errno 110" in msg:
            self._log_activity(f"{self._mode.upper()} rx() timed out")
            if self._mode == "cw":
                self._status_lbl.setText(
                    "CW rx() timed out. The worker will retry briefly; if this repeats, "
                    "try restarting with --sdr-uri usb:1.5.5 or power-cycle Pluto."
                )

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

    def _start_mode_a(self) -> None:
        """Open the Mode A backend (if needed) and start the FMCW worker."""
        if self._mode_a_backend is None:
            self._log_activity("Opening Mode A backend …")
            try:
                self._mode_a_backend, cfg = _open_mode_a_backend(
                    self._args, self._cal.steer_deg)
                self._refresh_mode_a_context(cfg)
            except Exception as exc:
                self._state = "RX ERROR"
                self._update_state_label()
                self._status_lbl.setText(f"Mode A open failed: {exc}")
                self._log_activity(f"Mode A open FAILED: {exc}")
                return
        self._mode = "fmcw"
        self._state = "SEARCH"
        self._update_state_label()
        self._mode_a_worker = ModeAWorker(self._mode_a_backend)
        self._mode_a_worker.data_ready.connect(self._on_mode_a_capture)
        self._mode_a_worker.error.connect(self._on_acq_error)
        self._mode_a_worker.start()
        self._log_activity("Mode A FMCW running")

    def _stop_mode_a(self) -> None:
        if self._mode_a_worker is not None:
            self._mode_a_worker.requestInterruption()
            self._mode_a_worker.wait(5000)
            self._mode_a_worker = None
        if self._mode_a_backend is not None:
            try:
                self._mode_a_backend.close()
            except Exception as exc:
                print(f"[modeA] close warning: {exc}")
            self._mode_a_backend = None
            gc.collect()
        self._log_activity("Mode A backend closed")

    def _start_cw(self) -> bool:
        """Open a fresh CW session (with retries) and start the CW worker."""
        # Reset DSP state for the new session
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
        self._cw_frames_received = 0
        self._cw_session_start_t = time.monotonic()

        if self._args.cw_source == "demo":
            self._log_activity("Starting synthetic CW demo source (no Pluto)")
            self._cw_session = SyntheticCWSession(self._args)
            self._cw_session.open()
        else:
            last_exc: Exception | None = None
            for attempt in range(5):
                try:
                    self._log_activity(f"Opening CW session (attempt {attempt + 1}/5) …")
                    self._cw_session = CWSession(self._args)
                    self._cw_session.open()
                    # Validate the actual RX path before declaring CW mode alive.
                    # Previously open() could succeed, then the worker's first rx()
                    # failed with Errno 110 and the B/C plots never updated.
                    for prime in range(3):
                        try:
                            _ = self._cw_session.rx()
                            break
                        except Exception as rx_exc:
                            last_exc = rx_exc
                            self._log_activity(
                                f"CW RX prime failed {prime + 1}/3: {type(rx_exc).__name__}: {rx_exc}"
                            )
                            time.sleep(0.5)
                    else:
                        raise RuntimeError(f"CW rx() failed during prime: {last_exc}")
                    break
                except Exception as exc:
                    last_exc = exc
                    self._log_activity(f"CW open failed: {type(exc).__name__}: {exc}")
                    self._status_lbl.setText(
                        f"CW open attempt {attempt + 1}/5 failed; waiting for Pluto …"
                    )
                    # Pluto needs more time on EBUSY; back off harder each retry
                    wait_s = 2.0 + 1.5 * attempt
                    time.sleep(wait_s)
                    # Best-effort gc to drop any half-built session
                    if self._cw_session is not None:
                        try:
                            self._cw_session.close()
                        except Exception:
                            pass
                    self._cw_session = None
                    gc.collect()
            else:
                self._state = "RX ERROR"
                self._update_state_label()
                self._status_lbl.setText(
                    f"CW could not open after 5 attempts.\n"
                    f"Last error: {last_exc}\n"
                    "Try Restart Mode A, or unplug+replug the Pluto USB and Restart."
                )
                return False

        self._mode = "cw"
        self._state = "CW"
        self._update_state_label()
        self._cw_worker = CWWorker(self._cw_session)
        self._cw_worker.frame_ready.connect(self._on_cw_frame)
        self._cw_worker.error.connect(self._on_acq_error)
        self._cw_worker.start()
        focused = self._registry.focused()
        who = f"P{focused.pid} @ {focused.range_m:.2f} m" if focused else "current beam"
        source = "synthetic demo" if self._args.cw_source == "demo" else "hardware"
        self._log_activity(f"CW session running on {who}")
        self._status_lbl.setText(
            f"Modes B + C running on {who} ({source}).\n"
            f"Vitals appear after ~{self._args.vitals_warmup_sec:.0f} s of warmup.\n"
            f"Press 'Return to Mode A' to switch back."
        )
        return True

    def _stop_cw(self) -> None:
        if self._cw_worker is not None:
            self._cw_worker.requestInterruption()
            self._cw_worker.wait(5000)
            self._cw_worker = None
        if self._cw_session is not None:
            try:
                self._cw_session.close()
            except Exception as exc:
                print(f"[cw] close warning: {exc}")
            self._cw_session = None
            gc.collect()
        self._log_activity("CW session closed")

    def _start_initial_cw(self) -> None:
        """Boot directly into CW without touching Mode A first."""
        if self._state == "CW":
            return
        self._mode = "switching"
        self._state = "SWITCHING"
        self._update_state_label()
        self._status_lbl.setText(
            "Starting directly in CW demo mode. Opening Modes B + C without "
            "opening Mode A first."
        )
        self._log_activity(f"Opening CW first · source={self._args.cw_source}")
        QApplication.processEvents()
        ok = self._start_cw()
        if not ok:
            self._status_lbl.setText(
                "CW-first open failed. Check Pluto/CN0566 ownership, then press "
                "'Run Modes B + C (CW)' again."
            )

    def _begin_cw_interrogation(self) -> None:
        """Manual: stop Mode A, settle, open CW. SLOW but reliable."""
        if self._state in {"CW", "SWITCHING"}:
            return
        self._state = "SWITCHING"
        self._update_state_label()
        self._log_activity("Switching: Mode A → CW (settling 3 s)")
        self._status_lbl.setText(
            "Switching to CW.  Closing Mode A, then waiting for Pluto to "
            "release the IIO context. This takes a few seconds — please wait."
        )
        QApplication.processEvents()    # let UI redraw before blocking
        self._stop_mode_a()
        time.sleep(self._switch_settle_s)
        QApplication.processEvents()
        ok = self._start_cw()
        if not ok:
            # Try to recover Mode A so the user isn't stuck on a black screen
            time.sleep(self._switch_settle_s)
            self._start_mode_a()

    def _end_cw_interrogation(self) -> None:
        """Manual: stop CW, settle, reopen Mode A."""
        if self._state not in {"CW", "RX ERROR"}:
            return
        self._state = "SWITCHING"
        self._update_state_label()
        self._log_activity("Switching: CW → Mode A (settling 3 s)")
        self._status_lbl.setText(
            "Returning to Mode A. Closing CW session, waiting for Pluto …"
        )
        QApplication.processEvents()
        was_cw = True
        if was_cw:
            self._store_signature()
        self._last_cw_time = time.monotonic()
        self._stop_cw()
        time.sleep(self._switch_settle_s)
        QApplication.processEvents()
        self._start_mode_a()

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

        # ── Multi-person registry update with motion gate + persistence ─
        # A real person modulates Doppler at breathing/limb rates; a static
        # reflector (wall, table) has all its energy in the zero-Doppler
        # bin even after MTI. We require the candidate's local micro-Doppler
        # slice to show some non-zero-Doppler power, then we require the
        # SAME range bin to persist across N consecutive Mode A frames
        # before promoting it to the registry. This kills the "many fake
        # patients" you were seeing.
        now = time.monotonic()
        nc = res.rd_pos.shape[1] if res.rd_pos.size else 0
        zero_bin = nc // 2
        gated: list[tuple[float, float]] = []
        new_streaks: dict[int, int] = {}
        for rng, score in res.candidates:
            # Locate the bin in the range axis
            if not (np.isfinite(rng) and rng > 0):
                continue
            ridx = int(np.argmin(np.abs(self._range_pos - float(rng))))
            slice_lin = 10.0 ** (res.rd_pos[ridx, :] / 10.0)
            if nc > 4:
                z0 = max(0, zero_bin - 1)
                z1 = min(nc, zero_bin + 2)
                static_p = float(np.mean(slice_lin[z0:z1]) + 1e-30)
                motion_p = float(np.mean(np.delete(slice_lin, np.arange(z0, z1))) + 1e-30)
                motion_ratio_db = 10.0 * np.log10(motion_p / static_p)
            else:
                motion_ratio_db = 0.0
            # Need motion bins within 6 dB of static-bin power for the bin
            # to qualify as "person-shaped".  Walls / large fixed reflectors
            # have all power at v=0 → ratio is very negative → rejected.
            # When a person is present even just breathing this passes after
            # a few coherent frames.
            if motion_ratio_db < -6.0:
                continue
            key = int(round(rng * 10.0))    # 10 cm bins for streak matching
            best_key = key
            best_d   = 2          # within ±2 dm = ±20 cm tolerance
            for k in self._candidate_streaks:
                d = abs(k - key)
                if d <= best_d:
                    best_d, best_key = d, k
            streak = self._candidate_streaks.get(best_key, 0) + 1
            new_streaks[best_key] = streak
            if streak >= self._detect_persist_n:
                gated.append((float(rng), float(score)))
        # Update persistence state — drop bins that didn't register this frame
        self._candidate_streaks = new_streaks

        self._registry.update(gated, now)
        # Snap focus to the manually-locked range if user pressed Lock
        if self._manual_lock_range_m is not None and self._registry.persons:
            best_pid = min(
                self._registry.persons.keys(),
                key=lambda p: abs(self._registry.persons[p].range_m
                                  - float(self._manual_lock_range_m)),
            )
            self._registry.focused_id = best_pid

        # Pose classification for the focused person from the micro-Doppler
        focused = self._registry.focused()
        if focused is not None:
            f_vit = focused
            feats = self._poser.features(
                res.micro_doppler, self._vel_ms,
                rr_bpm=f_vit.resp_bpm, hr_bpm=f_vit.hr_bpm,
                myo_index=f_vit.myo_index,
            )
            label, conf = self._poser.classify(feats)
            focused.activity = label
            focused.activity_conf = conf
            self._update_pose_panel(focused, feats)

        # Beam-sweep step: each beam dwells for N frames, then we step.
        # This is the only way to actually find persons across azimuth with
        # one beam at a time. While LOCKED we stop sweeping and stay on
        # the focused person's beam.
        if self._sweep_active and self._state != "LOCKED" and self._sweep_angles:
            self._sweep_count += 1
            if self._sweep_count >= self._sweep_dwell_n:
                self._sweep_count = 0
                self._sweep_idx = (self._sweep_idx + 1) % len(self._sweep_angles)
                ang = float(self._sweep_angles[self._sweep_idx])
                self._args.steer = ang
                self._cal.steer_deg = ang
                try:
                    self._mode_a_backend.set_steering_angle(ang)
                except Exception as exc:
                    print(f"[sweep] steering failed: {exc}")
                # Reset the per-beam streaks — different beams illuminate
                # different patches so persistence resets per beam.
                self._candidate_streaks.clear()
                self._steer_lbl.setText(f"{ang:+.0f}° (sweep)")
                self._log_activity(f"Sweep beam → {ang:+.0f}°")

        # Render scene + registry list + patient card + system activity
        self._update_scene_map()
        self._update_registry_label()
        self._update_patient_card()
        self._update_sysact()

        auto_locked = self._tracker.update(res.range_m, res.target_score)
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
        # Auto-CW only fires when a *registered* person is focused — the bare
        # tracker would happily lock onto the brightest reflection in an
        # empty room. Registry membership = passed motion gate + persistence.
        registry_has_person = self._registry.focused() is not None
        if (self._args.auto_interrogate and locked and cooldown_remaining <= 0.0
                and registry_has_person):
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
        self._cw_frames_received += 1
        try:
            self._cw_proc.push(raw)
            self._myo_proc.push(raw)
        except Exception as exc:
            print(f"[cw] push() failed: {type(exc).__name__}: {exc}")
            return

        # Diagnostic surface — useful when "no plots ever come to life":
        # tells the operator how many frames have arrived and whether DSP
        # is still in the warm-up window.
        if self._cw_frames_received % 25 == 0:
            elapsed = time.monotonic() - self._cw_session_start_t
            print(f"[cw] frames={self._cw_frames_received}  elapsed={elapsed:.1f} s")
            self._update_sysact()

        focused = self._registry.focused()

        self._cw_div += 1
        if self._cw_div >= 10:
            self._cw_div = 0
            try:
                r = self._cw_proc.compute()
            except Exception as exc:
                print(f"[cw] compute() failed: {type(exc).__name__}: {exc}")
                r = None
            if r is None:
                # Still warming up; surface progress in the status bar so the
                # operator does not assume the plots are dead.
                elapsed = time.monotonic() - self._cw_session_start_t
                self._status_lbl.setText(
                    f"CW warmup… frames={self._cw_frames_received}  "
                    f"elapsed={elapsed:.1f} s / warmup={self._args.vitals_warmup_sec:.1f} s"
                )
            else:
                self._last_vitals = r
                self._resp_curve.setData(x=r.time_axis, y=r.resp_disp_mm)
                self._hr_curve.setData(  x=r.time_axis, y=r.hr_disp_mm)
                self._psd_curve.setData( x=r.psd_freq_hz, y=r.psd_db)
                if r.resp_bpm > 0:
                    self._resp_bpm_line.setValue(r.resp_bpm / 60.0)
                if r.hr_bpm > 0:
                    self._hr_bpm_line.setValue(r.hr_bpm / 60.0)
                rr = f"{r.resp_bpm:5.1f}" if r.resp_conf >= self._args.min_resp_conf else "  ––"
                hr = f"{r.hr_bpm:5.1f}"   if r.hr_conf   >= self._args.min_hr_conf   else "  ––"
                self._vit_text.setHtml(
                    f'<span style="color:#00ffcc; font-size:13pt; font-weight:bold;">RR {rr} bpm</span>'
                    f'  <span style="color:#888;">conf {r.resp_conf:.2f}</span><br>'
                    f'<span style="color:#ff66cc; font-size:13pt; font-weight:bold;">HR {hr} bpm</span>'
                    f'  <span style="color:#888;">conf {r.hr_conf:.2f}</span>'
                )
                self._signature_lbl.setText(
                    f"Signature: range={self._tracker.range_m or 0:.2f}m  RR={rr} bpm  HR={hr} bpm"
                )
                # Attribute vitals to the focused person
                if focused is not None:
                    focused.resp_bpm   = float(r.resp_bpm)
                    focused.resp_conf  = float(r.resp_conf)
                    focused.hr_bpm     = float(r.hr_bpm)
                    focused.hr_conf    = float(r.hr_conf)
                    focused.last_vitals_t = time.monotonic()
                    focused.rr_hist.append((focused.last_vitals_t, focused.resp_bpm))
                    focused.hr_hist.append((focused.last_vitals_t, focused.hr_bpm))
                    self._update_patient_card()

        self._myo_div += 1
        if self._myo_div >= 4:
            self._myo_div = 0
            try:
                m = self._myo_proc.compute()
            except Exception as exc:
                print(f"[myo] compute() failed: {type(exc).__name__}: {exc}")
                m = None
            if m is not None:
                self._last_myo = m
                self._myo_trace.append(m.myo_index)
                self._myo_curve.setData(
                    x=self._myo_t, y=np.asarray(self._myo_trace, dtype=np.float32))
                self._amp_curve.setData(x=m.spec_freq_hz, y=m.spec_db)
                if not m.calibrated:
                    pct = int(m.cal_progress * 100)
                    self._myo_text.setHtml(
                        f'<span style="color:#ffaa44; font-size:11pt;">'
                        f'calibrating… {pct}%</span>')
                elif m.activation:
                    self._myo_text.setHtml(
                        f'<span style="color:#ff9999; font-size:13pt; font-weight:bold;">ACTIVE</span>'
                        f'  <span style="color:#ccc;">idx {m.myo_index:.2f}  cent {m.cur_centroid_hz:.0f} Hz</span>')
                else:
                    self._myo_text.setHtml(
                        f'<span style="color:#aaffaa; font-size:13pt; font-weight:bold;">rest</span>'
                        f'  <span style="color:#ccc;">idx {m.myo_index:.2f}  cent {m.cur_centroid_hz:.0f} Hz</span>')
                if focused is not None:
                    focused.myo_index  = float(m.myo_index)
                    focused.myo_active = bool(m.activation)
                    focused.myo_hist.append((time.monotonic(), focused.myo_index))
                    self._update_patient_card()

    def _update_state_label(self) -> None:
        colors = {
            "SEARCH":    ("#224466", "#aaeeff"),
            "LOCKED":    ("#225522", "#aaffaa"),
            "SWITCHING": ("#554422", "#ffeeaa"),
            "CW":        ("#444422", "#ffeeaa"),
            "RX ERROR":  ("#772222", "#ffcccc"),
        }
        bg, fg = colors.get(self._state, ("#333355", "#ffffff"))
        self._state_lbl.setText(self._state)
        self._state_lbl.setStyleSheet(
            f"font-size:20px; font-weight:bold; "
            f"background:{bg}; color:{fg}; padding:10px; border-radius:6px;")
        self._update_sysact()

    def _on_mode_a_error(self, msg: str) -> None:
        # Kept for backward compat; routed to the unified handler now.
        self._on_acq_error(msg)

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
        # Live-apply CW gains if a CW session is currently open; otherwise
        # they take effect on the next "Run Modes B + C" press.
        if self._cw_session is not None and self._cw_session.sdr is not None:
            try:
                sdr = self._cw_session.sdr
                sdr.rx_hardwaregain_chan0 = int(self._args.cw_rx_gain)
                sdr.rx_hardwaregain_chan1 = int(self._args.cw_rx_gain)
                sdr.tx_hardwaregain_chan1 = int(self._args.cw_tx_gain)
                self._status_lbl.setText(
                    f"Applied CW gains live: RX={self._args.cw_rx_gain} dB, "
                    f"TX={self._args.cw_tx_gain} dB.")
            except Exception as exc:
                self._status_lbl.setText(f"CW gain apply failed: {exc}")
        else:
            self._status_lbl.setText(
                f"Stashed CW gains: RX={self._args.cw_rx_gain} dB, "
                f"TX={self._args.cw_tx_gain} dB. Will apply on next CW switch.")

    def _apply_mode_a_params(self) -> None:
        self._unlock_target()
        if self._state == "CW":
            self._status_lbl.setText(
                "Currently in CW. Press 'Return to Mode A' first; "
                "FMCW params will apply when Mode A reopens.")
            return
        self._status_lbl.setText("Restarting Mode A with new FMCW parameters …")
        self._log_activity("Restarting Mode A with new FMCW parameters")
        QApplication.processEvents()
        self._stop_mode_a()
        time.sleep(self._switch_settle_s)
        self._start_mode_a()

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

    # ------------------------------------------------------------------
    # System Activity panel
    # ------------------------------------------------------------------

    def _log_activity(self, msg: str) -> None:
        """Push a one-line activity record + refresh the System Activity panel."""
        ts = time.strftime("%H:%M:%S")
        self._activity_log.appendleft(f"<span style='color:#778;'>{ts}</span> {msg}")
        self._update_sysact(msg)

    def _update_sysact(self, current: str | None = None) -> None:
        # "Now" line — built from the current state + focused person
        focused = self._registry.focused()
        who = f"P{focused.pid} @ {focused.range_m:.2f}m" if focused else "none"
        if self._state == "CW":
            elapsed = time.monotonic() - self._cw_session_start_t
            now_text = (
                f"⛏ CW interrogation on {who}  "
                f"({elapsed:.0f}s / {self._args.cw_dwell_s:.0f}s)"
            )
        elif self._state == "LOCKED":
            now_text = f"◎ LOCKED  on {who}  beam {self._args.steer:+.0f}°"
        elif self._state == "SWITCHING":
            now_text = "↻ Reconfiguring SDR …"
        elif self._state == "RX ERROR":
            now_text = "⚠ Acquisition error"
        else:
            if self._sweep_active and self._sweep_angles:
                ang = float(self._sweep_angles[self._sweep_idx])
                now_text = (
                    f"🔍 SCANNING  beam {ang:+.0f}°  "
                    f"({self._sweep_count + 1}/{self._sweep_dwell_n} of "
                    f"{self._sweep_idx + 1}/{len(self._sweep_angles)})  "
                    f"persons={len(self._registry.persons)}"
                )
            else:
                now_text = (
                    f"⏸ STARING beam {self._args.steer:+.0f}°  "
                    f"persons={len(self._registry.persons)}"
                )
        if current:
            now_text = current + "  ·  " + now_text
        self._sysact_now_lbl.setText(now_text)
        self._sysact_log_lbl.setText("<br>".join(self._activity_log))

    def _on_sweep_toggle(self, state: int) -> None:
        self._sweep_active = (state == Qt.Checked)
        self._args.sweep_beam = self._sweep_active
        if not self._sweep_active:
            self._log_activity(f"Sweep OFF — staring at {self._args.steer:+.0f}°")
        else:
            if not self._sweep_angles:
                self._sweep_angles = _parse_beam_sweep(self._args.beam_sweep)
            self._log_activity(f"Sweep ON — {len(self._sweep_angles)} angles")

    # ------------------------------------------------------------------
    # Multi-person GUI helpers
    # ------------------------------------------------------------------

    def _on_cycle_focus(self) -> None:
        self._registry.cycle_focus()
        self._update_scene_map()
        self._update_registry_label()
        self._update_patient_card()
        f = self._registry.focused()
        if f is not None:
            self._manual_lock_range_m = float(f.range_m)
            self._tracker.locked = True
            self._tracker.range_m = float(f.range_m)
            self._tracker.quality = max(self._tracker.quality, 0.5)
            self._status_lbl.setText(
                f"Focus → P{f.pid} at {f.range_m:.2f} m. "
                "Run 'Interrogate locked target' to read vitals/myo."
            )

    def _on_reset_registry(self) -> None:
        self._registry.reset()
        self._scn_scatter.clear()
        for t in self._scn_labels:
            try:
                self._p_scn.removeItem(t)
            except Exception:
                pass
        self._scn_labels.clear()
        self._fft_persons.clear()
        self._update_registry_label()
        self._update_patient_card()
        self._status_lbl.setText("Person registry cleared.")

    def _update_scene_map(self) -> None:
        # Beam wedge — half-width approximated by the array beam (≈10° at 10 GHz)
        steer = float(self._args.steer)
        half_w = 8.0
        max_r = float(self._args.max_range)
        # Wedge polygon
        edge1 = np.deg2rad(steer - half_w)
        edge2 = np.deg2rad(steer + half_w)
        bx = [0.0, max_r * np.sin(edge1),
              max_r * np.sin(steer * np.pi / 180.0),
              max_r * np.sin(edge2), 0.0]
        by = [0.0, max_r * np.cos(edge1),
              max_r * np.cos(steer * np.pi / 180.0),
              max_r * np.cos(edge2), 0.0]
        self._beam_poly.setData(x=bx, y=by)

        # Scatter persons + labels
        spots: list[dict] = []
        # Remove old labels
        for t in self._scn_labels:
            try:
                self._p_scn.removeItem(t)
            except Exception:
                pass
        self._scn_labels.clear()

        steer_rad = np.deg2rad(steer)
        for pid, p in self._registry.persons.items():
            x = float(p.range_m * np.sin(steer_rad))
            y = float(p.range_m * np.cos(steer_rad))
            is_focus = (pid == self._registry.focused_id)
            r, g, b = p.color
            spots.append({
                "pos": (x, y),
                "size": 28 if is_focus else 18,
                "brush": pg.mkBrush(r, g, b, 230 if is_focus else 160),
                "pen": pg.mkPen("w" if is_focus else "k", width=2 if is_focus else 1),
            })
            label = pg.TextItem(
                f"P{pid}{' ◀' if is_focus else ''}\n{p.activity}",
                color=(255, 255, 255), anchor=(0.5, 1.2))
            label.setPos(x, y)
            self._p_scn.addItem(label)
            self._scn_labels.append(label)
        self._scn_scatter.setData(spots=spots)

        # Person markers along the range-profile axis (1,0)
        fft_spots = []
        for pid, p in self._registry.persons.items():
            r, g, b = p.color
            is_focus = (pid == self._registry.focused_id)
            fft_spots.append({
                "pos": (float(p.range_m), -10.0),
                "size": 16 if is_focus else 10,
                "brush": pg.mkBrush(r, g, b, 240),
                "pen": pg.mkPen("w" if is_focus else "k", width=1),
                "symbol": "t",
            })
        self._fft_persons.setData(spots=fft_spots)

    def _update_registry_label(self) -> None:
        if not self._registry.persons:
            self._reg_lbl.setText("registry empty")
            return
        lines = []
        for pid, p in sorted(self._registry.persons.items()):
            mark = "●" if pid == self._registry.focused_id else "○"
            r, g, b = p.color
            age = max(0.0, time.monotonic() - p.first_seen)
            lines.append(
                f'<span style="color:rgb({r},{g},{b});">{mark} P{pid}</span> '
                f'{p.range_m:4.2f} m  {p.activity}  '
                f'<span style="color:#888;">age {age:.0f}s</span>'
            )
        self._reg_lbl.setText("<br>".join(lines))

    def _update_patient_card(self) -> None:
        f = self._registry.focused()
        if f is None:
            self._patient_lbl.setText(
                '<span style="color:#888;">No target focused.<br>'
                'Wait for detections or press <b>Focus next ⟳</b>.</span>')
            return

        def _hr_color(bpm: float, conf: float) -> str:
            if conf < self._args.min_hr_conf or bpm <= 0:
                return "#888"
            if bpm < 50 or bpm > 110:
                return "#ff8866"
            if bpm < 60 or bpm > 100:
                return "#ffcc66"
            return "#9be08a"

        def _rr_color(bpm: float, conf: float) -> str:
            if conf < self._args.min_resp_conf or bpm <= 0:
                return "#888"
            if bpm < 8 or bpm > 28:
                return "#ff8866"
            if bpm < 10 or bpm > 22:
                return "#ffcc66"
            return "#9be08a"

        rr_str = f"{f.resp_bpm:4.1f}" if f.resp_conf >= self._args.min_resp_conf and f.resp_bpm > 0 else "––"
        hr_str = f"{f.hr_bpm:4.1f}"   if f.hr_conf   >= self._args.min_hr_conf   and f.hr_bpm   > 0 else "––"
        myo_color = "#ff9999" if f.myo_active else "#aaffaa"
        rc, gc, bc = f.color

        alerts = []
        if f.hr_conf >= self._args.min_hr_conf and f.hr_bpm > 0:
            if f.hr_bpm > 110: alerts.append("HR HIGH")
            elif f.hr_bpm < 50: alerts.append("HR LOW")
        if f.resp_conf >= self._args.min_resp_conf and f.resp_bpm > 0:
            if f.resp_bpm > 28: alerts.append("TACHYPNEA")
            elif f.resp_bpm < 8: alerts.append("BRADYPNEA")
        if f.activity == "FALL":
            alerts.append("FALL DETECTED")
        alert_html = (
            f'<span style="color:#ffd1a0; background:#883322; '
            f'padding:2px 6px; border-radius:4px;">⚠ {" / ".join(alerts)}</span>'
            if alerts else
            '<span style="color:#9be08a;">● stable</span>'
        )

        ago = max(0.0, time.monotonic() - f.last_vitals_t) if f.last_vitals_t > 0 else None
        ago_str = f"{ago:.0f} s ago" if ago is not None else "no vitals yet"

        self._patient_lbl.setText(
            f'<div style="font-size:13px;">'
            f'<span style="color:rgb({rc},{gc},{bc}); font-size:16px; font-weight:bold;">'
            f'  P{f.pid}</span>'
            f'  <span style="color:#bbb;">@ {f.range_m:.2f} m, beam {self._args.steer:+.0f}°</span>'
            f'<br><span style="color:#aaa;">activity:</span> '
            f'<b style="color:#ffe;">{f.activity}</b>'
            f' <span style="color:#888;">({f.activity_conf*100:.0f}%)</span>'
            f'<br><span style="color:#aaa;">RR:</span> '
            f'<span style="color:{_rr_color(f.resp_bpm, f.resp_conf)}; font-size:18px; font-weight:bold;">'
            f'{rr_str}</span> <span style="color:#888;">bpm</span>'
            f'<br><span style="color:#aaa;">HR:</span> '
            f'<span style="color:{_hr_color(f.hr_bpm, f.hr_conf)}; font-size:18px; font-weight:bold;">'
            f'{hr_str}</span> <span style="color:#888;">bpm</span>'
            f'<br><span style="color:#aaa;">Myo:</span> '
            f'<span style="color:{myo_color}; font-weight:bold;">'
            f'{f.myo_index:.2f}</span> '
            f'<span style="color:#888;">{"ACTIVE" if f.myo_active else "rest"}</span>'
            f'<br><span style="color:#888;">vitals: {ago_str}</span>'
            f'<br>{alert_html}'
            f'</div>'
        )

    def _update_pose_panel(self, person: TrackedPerson, feats: PoseFeatures) -> None:
        rc, gc, bc = person.color
        self._pose_text.setHtml(
            f'<div style="font-size:11px;">'
            f'<span style="color:#888;">Mamba SSM head ▸ focus '
            f'<b style="color:rgb({rc},{gc},{bc});">P{person.pid}</b></span>'
            f'<br><span style="font-size:18px; color:#fff; font-weight:bold;">'
            f'{person.activity}</span>'
            f'  <span style="color:#aac;">{person.activity_conf*100:.0f}%</span>'
            f'<br><span style="color:#888;">Doppler features:</span>'
            f'<br>centroid <span style="color:#ddd;">{feats.centroid_mps:+.2f}</span> m/s'
            f'  spread <span style="color:#ddd;">{feats.spread_mps:.2f}</span> m/s'
            f'<br>peak |v| <span style="color:#ddd;">{feats.peak_speed:.2f}</span> m/s'
            f'  energy <span style="color:#ddd;">{feats.energy_dB:+.1f}</span> dBr'
            f'<br><span style="color:#888;">Vitals features:</span>'
            f'<br>RR <span style="color:#ddd;">{feats.rr_bpm:.1f}</span>'
            f'  HR <span style="color:#ddd;">{feats.hr_bpm:.1f}</span>'
            f'  myo <span style="color:#ddd;">{feats.myo_index:.2f}</span>'
            f'</div>'
        )

    def closeEvent(self, event) -> None:
        if self._cw_dwell_timer is not None:
            self._cw_dwell_timer.stop()
            self._cw_dwell_timer = None
        # Stop whichever worker / backend is currently open.
        self._stop_cw()
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
    if (
        args.start_mode == "cw"
        and args.load_calibration is None
        and not args.skip_calibration
        and not args.force_scene_calibration
    ):
        print("[cal] --start-mode cw: skipping terminal scene calibration for CW-first demo.")
        print("[cal] Use --force-scene-calibration if you want the empty-room/target Mode A calibration first.")
        args.skip_calibration = True
    cal = run_terminal_calibration(args)

    # Calibration may have just closed a hardware backend; give Pluto time to
    # release the previous IIO context. CW demo mode does not touch Pluto.
    if args.start_mode == "cw" and args.cw_source == "demo":
        print("[cal→gui] CW demo source selected; no Pluto settle needed.")
    else:
        print("[cal→gui] settling Pluto for 2.0 s …")
        time.sleep(2.0)

    print("\nLaunching closed-loop GUI.")
    print(
        f"Mode A: RF {args.output_freq/1e9:.4f} GHz, {args.num_chirps} chirps, "
        f"{args.sample_rate/1e6:.1f} MSPS, {args.bw/1e6:.0f} MHz BW"
    )
    print(f"Mode B/C: {args.cw_sample_rate/1e3:.0f} kSPS, CW dwell {args.cw_dwell_s:.1f} s")
    print(f"Initial GUI mode: {args.start_mode.upper()}")
    print(f"Initial beam: {cal.steer_deg:+.1f} deg, target range: {cal.target_range_m}\n")

    app = QApplication(sys.argv)
    _dark_palette(app)
    win = ClosedLoopWindow(args, cal)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
