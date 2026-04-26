"""SENTINEL — Tri-Mode Phased-Array Radar Orchestrator (no ML / inference).

Single integrated entry point that brings up the CN0566+Pluto once, then
*schedules* between three operating modes on the same hardware:

  Mode A — TDD-burst FMCW Range-Doppler  (HAR / posture / falls proxy)
  Mode B — CW micro-Doppler vitals       (respiration + heartbeat)
  Mode C — CW reflectometry myo-index    (EMG-proxy / muscle activity trends)

Modes B and C share the same CW hardware configuration, so during a single
"CW window" both DSP pipelines (CWProcessor + MyoProcessor) consume the same
IQ stream in parallel.  Mode A reprograms the ADF4159 into single_sawtooth_burst
ramp mode and uses the Pluto TDD engine with soft-sync to capture coherent
chirp bursts (matches sentinel_mode_a_v5.py — known stable on Windows).

Schedule
--------
  Default: 8 s FMCW → 20 s CW → 8 s FMCW → ...
  Sidebar combo box pins to a single mode and disables cycling.
  Schedule dwells are sliders so you can dial them at runtime.

Mode switching
--------------
  TX cyclic CW tone is left running across mode switches; only:
    1) phaser.ramp_mode      ("single_sawtooth_burst" | "disabled")
    2) phaser PLL deviation registers  (set for FMCW, harmless for CW)
    3) phaser.enable = 0     (latch new register set)
    4) sdr.rx_buffer_size    (chirp-burst sized vs CW-frame sized)
    5) tdd.enable           (armed only inside acquire() in FMCW mode)

  Acquisition worker is paused before the switch and resumed after.

GUI (2×3 plot grid + sidebar)
-----------------------------
  ┌─ Range-Doppler heatmap (A) ─┬─ Vitals displacement (B) ─┬─ Myo-index trace (C) ─┐
  │                              │                            │                       │
  ├─ Range profile + targets (A) ┼─ Phase PSD 0–4 Hz (B) ─────┼─ Amplitude PSD (C) ───┤
  │ + motion-energy gauge        │                            │ + EMG band shading    │
  └──────────────────────────────┴────────────────────────────┴───────────────────────┘

Run
---
  python sentinel.py                           # auto-cycle, default schedule
  python sentinel.py --pin cw                  # CW only (Mode B+C parallel)
  python sentinel.py --pin fmcw                # FMCW only (Mode A)
  python sentinel.py --num-chirps 128 --ramp-time-us 400
"""
from __future__ import annotations

import argparse
import collections
import gc
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from cw_dsp  import CWProcessor, VitalsResult
from myo_dsp import MyoProcessor, MyoResult


# ---------------------------------------------------------------------------
# Theme + helpers
# ---------------------------------------------------------------------------

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
pg.setConfigOption("background", "#1a1a2e")
pg.setConfigOption("foreground", "#dddddd")

LABEL_KW         = {"color": "#ccc", "font-size": "10pt"}
CHEBYSHEV_TAPER  = (8, 34, 84, 127, 127, 84, 34, 8)
SPEED_OF_LIGHT   = 299_792_458.0


def _viridis_lut() -> np.ndarray:
    try:
        cmap = pg.colormap.get("viridis", source="matplotlib")
    except Exception:
        cmap = pg.colormap.get("viridis")
    return cmap.getLookupTable(0.0, 1.0, 256)

VIRIDIS_LUT = _viridis_lut()


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel",
        description="SENTINEL: tri-mode phased-array radar orchestrator (FMCW + CW vitals + CW myo)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    hw = p.add_argument_group("hardware URIs")
    hw.add_argument("--rpi-uri", default="ip:phaser.local")
    hw.add_argument("--sdr-uri", default="ip:192.168.2.1",
                    help="Pluto IIO URI. Do NOT use ip:phaser.local:50901.")
    hw.add_argument("--tdd-uri", default=None,
                    help="TDD engine URI (defaults to --sdr-uri)")

    r = p.add_argument_group("RF (shared)")
    r.add_argument("--output-freq", type=float, default=10.25e9)
    r.add_argument("--center-freq", type=float, default=2.1e9)
    r.add_argument("--signal-freq", type=float, default=100e3)
    r.add_argument("--sample-rate", type=float, default=4e6,
                   help="Mode A FMCW Pluto ADC/DAC sample rate. 4 MSPS matches the proven TDD path.")
    r.add_argument("--rx-gain",     type=int,   default=45)
    r.add_argument("--tx-gain",     type=int,   default=0)
    r.add_argument("--gain-taper",  default=",".join(str(g) for g in CHEBYSHEV_TAPER),
                   metavar="g0,..,g7")
    r.add_argument("--steer", type=float, default=0.0, metavar="DEG")

    a = p.add_argument_group("Mode A (TDD FMCW)")
    a.add_argument("--chirp-bw",       type=float, default=500e6)
    a.add_argument("--ramp-time-us",   type=float, default=300.0)
    a.add_argument("--num-chirps",     type=int,   default=64)
    a.add_argument("--frame-guard-ms", type=float, default=0.2,
                   help="Inter-chirp guard (ms) added to ramp time for TDD frame")
    a.add_argument("--ramp-good-frac", type=float, default=0.9,
                   help="Fraction of ramp considered usable (excludes settling)")
    a.add_argument("--tdd-arm-delay-s", type=float, default=0.05,
                   help="Wait before sync_soft to ensure DMA is armed")
    a.add_argument("--tdd-rx-timeout-ms", type=int, default=30000)
    a.add_argument("--mti", action=argparse.BooleanOptionalAction, default=True,
                   help="Subtract chirp-mean before Doppler FFT (clutter suppression)")
    a.add_argument("--window", action=argparse.BooleanOptionalAction, default=True,
                   help="Apply range/Doppler Blackman windows before FFT")
    a.add_argument("--min-range-m", type=float, default=0.5,
                   help="Ignore closer bins when selecting the target")
    a.add_argument("--max-display-range-m", type=float, default=10.0,
                   help="Initial Mode A displayed range")
    a.add_argument("--suppress-leakage", action=argparse.BooleanOptionalAction, default=True,
                   help="Blank near-zero range bins in Mode A displays")
    a.add_argument("--zero-blank-bins", type=int, default=2,
                   help="Number of near-zero positive-range bins to blank when leakage suppression is on")

    cw = p.add_argument_group("Mode B + C (CW)")
    cw.add_argument("--cw-sample-rate", type=float, default=600e3,
                    help="Mode B/C CW Pluto ADC/DAC sample rate. 600 kSPS matches the standalone CW modes.")
    cw.add_argument("--cw-frame-size", type=int, default=32768,
                    help="Samples per CW rx() call")
    cw.add_argument("--cw-history-sec", type=float, default=20.0)
    cw.add_argument("--cw-phase-fs", type=float, default=100.0)
    cw.add_argument("--vitals-warmup-sec", type=float, default=8.0,
                    help="Phase history required before showing vitals")
    cw.add_argument("--min-resp-conf", type=float, default=0.05,
                    help="Minimum confidence before showing RR")
    cw.add_argument("--min-hr-conf", type=float, default=0.12,
                    help="Minimum confidence before showing HR")
    cw.add_argument("--myo-phase-fs", type=float, default=2000.0)
    cw.add_argument("--myo-history-sec", type=float, default=10.0)
    cw.add_argument("--myo-baseline-sec", type=float, default=3.0)
    cw.add_argument("--myo-win-sec",   type=float, default=0.25)
    cw.add_argument("--myo-band-lo",   type=float, default=10.0)
    cw.add_argument("--myo-band-hi",   type=float, default=500.0)
    cw.add_argument("--myo-threshold", type=float, default=0.3)

    s = p.add_argument_group("scheduling")
    s.add_argument("--dwell-fmcw", type=float, default=8.0)
    s.add_argument("--dwell-cw",   type=float, default=20.0,
                   help="CW dwell should exceed vitals warmup so Mode B can populate")
    s.add_argument("--pin", choices=["fmcw", "cw"], default=None,
                   help="Skip cycling and stay in one mode")
    s.add_argument("--reopen-on-switch", action=argparse.BooleanOptionalAction, default=True,
                   help="Reopen Pluto/CN0566 contexts when changing FMCW<->CW. Slower, but avoids TDD-to-CW DMA wedges.")
    s.add_argument("--reopen-delay-s", type=float, default=0.75,
                   help="Delay after closing contexts before reopening on a mode switch")

    rec = p.add_argument_group("recording")
    rec.add_argument("--save-dir", type=Path, default=Path("recordings"))
    rec.add_argument("--no-record", action="store_true")
    return p


# ---------------------------------------------------------------------------
# Hardware controller
# ---------------------------------------------------------------------------

class HardwareController:
    """Owns sdr+phaser+TDD; switches between FMCW (TDD burst) and CW."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args   = args
        self.sdr    = None
        self.phaser = None
        self._tdd   = None
        self._adi   = None
        self.mode: str | None = None    # 'fmcw' | 'cw' | None
        self._target_mode = "fmcw"
        self._io_lock = threading.RLock()

        # FMCW timing (filled when set_mode_fmcw_tdd() runs)
        self.n_frame: int = 0           # samples per ramp PRI
        self.spc:     int = 0           # good samples per chirp
        self.start_offset: int = 0      # samples to skip at chirp start
        self.fmcw_buf_size: int = 0

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    def connect(self, target_mode: str = "fmcw") -> None:
        with self._io_lock:
            self._connect_unlocked(target_mode=target_mode)

    def _sample_rate_for_mode(self, mode: str | None = None) -> float:
        mode = mode or self._target_mode
        return float(self.args.cw_sample_rate if mode == "cw" else self.args.sample_rate)

    def _connect_unlocked(self, target_mode: str = "fmcw") -> None:
        self._target_mode = target_mode
        try:
            import adi
        except ImportError as exc:
            raise RuntimeError("pyadi-iio is required.") from exc
        self._adi = adi

        taper = tuple(int(v.strip())
                      for v in self.args.gain_taper.split(",") if v.strip())
        if len(taper) != 8:
            raise ValueError("--gain-taper must have 8 values")

        self.sdr    = adi.ad9361(uri=self.args.sdr_uri)
        self.sdr._ctx.set_timeout(int(self.args.tdd_rx_timeout_ms))
        self.phaser = adi.CN0566(uri=self.args.rpi_uri, sdr=self.sdr)

        self._configure_array(taper)
        self._configure_sdr_static()
        self._start_tx_tone()

        # Only FMCW needs the Pluto TDD helper. CW contexts are kept clean to
        # match the standalone Mode B/C bring-up path.
        if target_mode == "fmcw":
            self._setup_tdd_engine(adi)
        else:
            self._tdd = None

    def reopen_for_mode(self, mode: str) -> None:
        """Close and reopen hardware contexts, then enter the requested mode."""
        with self._io_lock:
            self._close_unlocked()
            time.sleep(max(0.0, float(self.args.reopen_delay_s)))
            self._connect_unlocked(target_mode=mode)
            if mode == "fmcw":
                self.set_mode_fmcw_tdd()
            elif mode == "cw":
                self.set_mode_cw()
            else:
                raise RuntimeError(f"Unknown mode {mode!r}")

    def _configure_array(self, taper) -> None:
        ph = self.phaser
        ph.configure(device_mode="rx")
        try:
            ph.element_spacing = 0.014
        except Exception:
            pass
        ph.load_gain_cal()
        ph.load_phase_cal()
        for ch in range(8):
            ph.set_chan_phase(ch, 0)
        for ch, g in enumerate(taper):
            ph.set_chan_gain(ch, g, apply_cal=True)
        for name, val in [("gpio_tx_sw", 0),
                          ("gpio_vctrl_1", 1),
                          ("gpio_vctrl_2", 1)]:
            self._gpio(name, val)

    def _gpio(self, name: str, val: int) -> None:
        for attr in ("_gpios", "gpios"):
            obj = getattr(self.phaser, attr, None)
            if obj is not None:
                setattr(obj, name, val)
                return

    def _configure_sdr_static(self) -> None:
        a, sdr = self.args, self.sdr
        sdr.sample_rate              = int(self._sample_rate_for_mode())
        sdr.rx_lo                    = int(a.center_freq)
        sdr.rx_enabled_channels      = [0, 1]
        sdr.gain_control_mode_chan0  = "manual"
        sdr.gain_control_mode_chan1  = "manual"
        sdr.rx_hardwaregain_chan0    = int(a.rx_gain)
        sdr.rx_hardwaregain_chan1    = int(a.rx_gain)
        sdr.tx_lo                    = int(a.center_freq)
        sdr.tx_enabled_channels      = [0, 1]
        sdr.tx_cyclic_buffer         = True
        sdr.tx_hardwaregain_chan0    = -88
        sdr.tx_hardwaregain_chan1    = int(a.tx_gain)

    def _start_tx_tone(self) -> None:
        a  = self.args
        N  = int(2 ** 18)
        fs = int(self._sample_rate_for_mode())
        fc = a.signal_freq
        t  = np.arange(N) / fs
        iq = (0.9 * (np.cos(2.0 * np.pi * t * fc) +
                     1j * np.sin(2.0 * np.pi * t * fc)) * 2 ** 14
              ).astype(np.complex64)
        try:
            self.sdr.tx_destroy_buffer()
        except Exception:
            pass
        try:
            self.sdr.tx([iq, iq])
        except OSError as exc:
            if exc.errno == 16:
                raise RuntimeError(
                    "TX buffer busy (errno=16). Unplug Pluto USB, wait 5 s, replug."
                ) from exc
            raise

    def _destroy_rx_buffer(self) -> None:
        """Drop stale DMA buffers before changing modes or rx_buffer_size."""
        if self.sdr is None:
            return
        try:
            self.sdr.rx_destroy_buffer()
        except Exception:
            pass

    def _close_unlocked(self) -> None:
        if self._tdd is not None:
            try:
                self._tdd.enable = False
            except Exception:
                pass
        if self.sdr is not None:
            self._destroy_rx_buffer()
            try:
                self.sdr.tx_destroy_buffer()
            except Exception:
                pass
        self._tdd = None
        self.phaser = None
        self.sdr = None
        self.mode = None
        gc.collect()

    def _setup_tdd_engine(self, adi_module) -> None:
        """Create the TDD engine handle (configured per-mode in set_mode_fmcw_tdd)."""
        tdd_uri = self.args.tdd_uri or self.args.sdr_uri
        try:
            sdr_pins = adi_module.one_bit_adc_dac(tdd_uri)
            try:
                sdr_pins.gpio_phaser_enable = True
            except Exception:
                pass
            try:
                # Disable external sync — we use soft-sync only
                if hasattr(sdr_pins, "gpio_tdd_ext_sync"):
                    sdr_pins.gpio_tdd_ext_sync = False
            except Exception:
                pass
        except Exception as exc:
            print(f"[tdd] one_bit_adc_dac setup failed: {exc}")
        self._tdd = adi_module.tddn(tdd_uri)

    # ------------------------------------------------------------------
    # Mode switching
    # ------------------------------------------------------------------

    def set_mode_cw(self) -> None:
        with self._io_lock:
            a, ph = self.args, self.phaser
            self._target_mode = "cw"
            self._destroy_rx_buffer()
            if self._tdd is not None:
                try:
                    self._tdd.enable = False
                except Exception:
                    pass
                try:
                    self._tdd.sync_external = False
                except Exception:
                    pass
                try:
                    self._tdd.sync_internal = False
                except Exception:
                    pass
                try:
                    self._tdd.sync_reset = True
                except Exception:
                    pass
            vco_freq = int(a.output_freq + a.signal_freq + a.center_freq)
            ph.frequency = int(vco_freq / 4)
            ph.ramp_mode = "disabled"
            try:
                ph.tx_trig_en = 0
            except Exception:
                pass
            ph.enable = 0
            self.sdr.rx_buffer_size = int(a.cw_frame_size)
            self.mode = "cw"

    def set_mode_fmcw_tdd(self) -> None:
        with self._io_lock:
            a, ph = self.args, self.phaser
            self._target_mode = "fmcw"

            # Disable TDD before reprogramming and drop the old DMA buffer.
            if self._tdd is not None:
                try:
                    self._tdd.enable = False
                except Exception:
                    pass
            self._destroy_rx_buffer()

            # Program ADF4159 — single_sawtooth_burst + tx_trig
            bw       = int(a.chirp_bw)
            steps    = max(1, int(a.ramp_time_us))   # 1 step per µs is stable
            vco_freq = int(a.output_freq + a.signal_freq + a.center_freq)

            ph.frequency        = int(vco_freq / 4)
            ph.freq_dev_range   = int(bw / 4)
            ph.freq_dev_step    = int((bw / 4) / steps)
            ph.freq_dev_time    = int(a.ramp_time_us)
            ph.delay_word       = 4095
            ph.delay_clk        = "PFD"
            ph.delay_start_en   = 0
            ph.ramp_delay_en    = 0
            ph.trig_delay_en    = 0
            ph.ramp_mode        = "single_sawtooth_burst"
            ph.sing_ful_tri     = 0
            ph.tx_trig_en       = 1
            ph.enable           = 0     # latch

            # Configure TDD engine for soft-sync burst.
            tdd = self._tdd
            if tdd is None and self._adi is not None:
                self._setup_tdd_engine(self._adi)
                tdd = self._tdd
            if tdd is None:
                raise RuntimeError("TDD engine not initialised")
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
            tdd.startup_delay_ms = 0
            pri_ms = a.ramp_time_us / 1e3 + a.frame_guard_ms
            tdd.frame_length_ms = pri_ms
            tdd.burst_count     = int(a.num_chirps)
            for ch in range(3):
                tdd.channel[ch].enable   = True
                tdd.channel[ch].polarity = False
                tdd.channel[ch].on_raw   = 0
                tdd.channel[ch].off_raw  = 10

            # Compute timing for chirp matrix slicing.
            fs            = float(self._sample_rate_for_mode("fmcw"))
            ramp_s        = a.ramp_time_us / 1e6
            good_s        = ramp_s * float(a.ramp_good_frac)
            offset_s      = ramp_s - good_s
            self.n_frame  = int(round(pri_ms / 1e3 * fs))
            self.spc      = max(8, int(round(good_s * fs)))
            self.start_offset = max(0, int(round(offset_s * fs)))

            # Buffer = next pow2 large enough for the whole burst (matches v5).
            total = self.n_frame * int(a.num_chirps)
            self.fmcw_buf_size = _next_pow2(int(total * 1.1) + 64)
            self.sdr.rx_buffer_size = self.fmcw_buf_size
            self.mode = "fmcw"

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def acquire(self):
        """Mode-aware single capture. Returns (ndarray, mode_tag)."""
        with self._io_lock:
            sdr = self.sdr
            mode = self.mode
            if mode is None:
                raise RuntimeError("No active acquisition mode")
            try:
                if mode == "fmcw" and self._tdd is not None:
                    raw = self._fmcw_capture_with_tdd()
                else:
                    raw = sdr.rx()
            except OSError as exc:
                if getattr(exc, "errno", None) == 110:
                    self.mode = None
                    raise RuntimeError(
                        "rx() returned host unreachable. Restart this app before retrying."
                    ) from exc
                raise
            if isinstance(raw, (list, tuple)):
                arr = (np.asarray(raw[0], dtype=np.complex64) +
                       np.asarray(raw[1], dtype=np.complex64))
            elif hasattr(raw, "ndim") and raw.ndim > 1:
                arr = (raw[0].astype(np.complex64) +
                       raw[1].astype(np.complex64))
            else:
                arr = np.asarray(raw, dtype=np.complex64).ravel()
            return arr, mode

    def _fmcw_capture_with_tdd(self):
        """Soft-sync TDD burst: arm DMA in thread, then trigger, then wait."""
        sdr = self.sdr
        result: dict = {}
        exc_box: list = []

        def _do_rx() -> None:
            try:
                result["data"] = sdr.rx()
            except Exception as e:
                exc_box.append(e)

        t = threading.Thread(target=_do_rx, daemon=True)
        t.start()
        time.sleep(max(0.0, float(self.args.tdd_arm_delay_s)))
        try:
            self._tdd.enable = True
            time.sleep(0.005)
            try:
                self._tdd.sync_soft = True
            except Exception:
                pass
        except Exception as exc:
            exc_box.append(exc)

        burst_s = self.n_frame * self.args.num_chirps / float(self._sample_rate_for_mode("fmcw"))
        timeout_s = max(
            10.0,
            self.args.tdd_rx_timeout_ms / 1000.0 + 5.0,
            burst_s * 5.0 + 1.0,
        )
        t.join(timeout=timeout_s)
        try:
            self._tdd.enable = False
        except Exception:
            pass

        if exc_box:
            exc = exc_box[0]
            if isinstance(exc, OSError) and getattr(exc, "errno", None) == 110:
                self.mode = None
                raise RuntimeError(
                    "FMCW TDD rx() returned host unreachable. "
                    "The Pluto data path is wedged; restart this app before retrying."
                ) from exc
            raise exc
        if "data" not in result:
            self.mode = None
            raise TimeoutError(
                f"FMCW TDD rx() did not complete within {timeout_s:.1f}s. "
                "The RX thread may still be blocked in libiio; restart this app before retrying."
            )
        return result["data"]

    def slice_chirp_matrix(self, raw: np.ndarray) -> np.ndarray:
        """Slice raw burst into (num_chirps, spc) complex matrix."""
        nc = int(self.args.num_chirps)
        spc = self.spc
        n_frame = self.n_frame
        offset = self.start_offset
        out = np.zeros((nc, spc), dtype=np.complex64)
        for k in range(nc):
            s = offset + k * n_frame
            e = s + spc
            if s >= raw.size:
                break
            chunk = raw[s:e]
            out[k, : chunk.size] = chunk
        return out

    # ------------------------------------------------------------------
    # Live tunables
    # ------------------------------------------------------------------

    def set_steer(self, deg: float) -> None:
        rad = np.radians(float(deg))
        delta = (2.0 * np.pi * self.args.output_freq * 0.014
                 * np.sin(rad) / SPEED_OF_LIGHT)
        with self._io_lock:
            try:
                self.phaser.set_beam_phase_diff(np.degrees(delta))
            except Exception as exc:
                print(f"[steer] {exc}")

    def set_rx_gain(self, gain_db: int) -> None:
        with self._io_lock:
            try:
                self.sdr.rx_hardwaregain_chan0 = int(gain_db)
                self.sdr.rx_hardwaregain_chan1 = int(gain_db)
            except Exception as exc:
                print(f"[rx-gain] {exc}")

    def set_tx_gain(self, gain_db: int) -> None:
        with self._io_lock:
            try:
                self.sdr.tx_hardwaregain_chan1 = int(gain_db)
            except Exception as exc:
                print(f"[tx-gain] {exc}")

    def teardown(self) -> None:
        with self._io_lock:
            self._close_unlocked()


# ---------------------------------------------------------------------------
# Acquisition worker
# ---------------------------------------------------------------------------

class AcqWorker(QThread):
    """Single sdr.rx() loop. Routes by mode tag; pause/resume around switches."""

    frame_ready = pyqtSignal(object, str)   # (ndarray, mode tag)
    error       = pyqtSignal(str)

    def __init__(self, controller: HardwareController) -> None:
        super().__init__()
        self._ctrl   = controller
        self._paused = threading.Event()
        self._paused.set()       # start paused until first mode set

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def run(self) -> None:
        consec_errors = 0
        while not self.isInterruptionRequested():
            if self._paused.is_set():
                time.sleep(0.02)
                continue
            if self._ctrl.mode is None:
                time.sleep(0.02)
                continue
            try:
                arr, mode = self._ctrl.acquire()
                self.frame_ready.emit(arr, mode)
                consec_errors = 0
            except Exception as exc:
                consec_errors += 1
                self.error.emit(f"{type(exc).__name__}: {exc}")
                if consec_errors >= 5:
                    break
                time.sleep(0.15)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class SentinelDashboard(QMainWindow):

    def __init__(self, controller: HardwareController, args: argparse.Namespace) -> None:
        super().__init__()
        self._ctrl = controller
        self._args = args

        # DSP processors
        self._cw_proc = CWProcessor(
            fs           = args.cw_sample_rate,
            signal_freq  = args.signal_freq,
            rf_freq      = args.output_freq,
            history_sec  = args.cw_history_sec,
            phase_fs     = args.cw_phase_fs,
            warmup_sec   = args.vitals_warmup_sec,
        )
        self._myo_proc = MyoProcessor(
            fs           = args.cw_sample_rate,
            signal_freq  = args.signal_freq,
            phase_fs     = args.myo_phase_fs,
            history_sec  = args.myo_history_sec,
            baseline_sec = args.myo_baseline_sec,
            win_sec      = args.myo_win_sec,
            band_lo      = args.myo_band_lo,
            band_hi      = args.myo_band_hi,
            threshold    = args.myo_threshold,
        )

        # Mode A axes (filled when first FMCW frame arrives)
        self._range_full: np.ndarray | None = None
        self._pos_mask: np.ndarray | None = None
        self._range_axis: np.ndarray | None = None
        self._velocity_axis: np.ndarray | None = None
        self._fmcw_axes_mode: tuple | None = None    # cache key

        # Motion-energy rolling history (FMCW) — 10 s window at ~5 updates/s
        mot_n = 100
        self._motion_history = collections.deque([0.0] * mot_n, maxlen=mot_n)
        self._motion_t       = np.linspace(-10.0, 0.0, mot_n, dtype=np.float32)

        # Myo trace
        myo_n = 200
        self._myo_history = collections.deque([0.0] * myo_n, maxlen=myo_n)
        self._myo_t = np.linspace(-args.myo_history_sec, 0.0, myo_n, dtype=np.float32)

        # Latest cached results (so panels keep showing values when mode is idle)
        self._last_vitals: VitalsResult | None = None
        self._last_myo:    MyoResult    | None = None
        self._last_target_m: float = 0.0
        self._last_motion: float = 0.0

        # RD display state
        self._rd_auto_leveled = False
        self._last_rd_db: np.ndarray | None = None

        # Compute throttling
        self._frame_count    = 0
        self._cw_compute_div = 0
        self._myo_compute_div = 0

        # Recording
        self._saving      = False
        self._rec_iq:     list[tuple[float, np.ndarray, str]] = []

        self.setWindowTitle(
            f"SENTINEL Tri-Mode  RF={args.output_freq/1e9:.3f} GHz  "
            f"A_SR={args.sample_rate/1e3:.0f} kHz  CW_SR={args.cw_sample_rate/1e3:.0f} kHz"
        )
        self._build_ui()
        self.showMaximized()

        # Mode scheduling
        self._pinned_mode = args.pin
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setSingleShot(True)
        self._schedule_timer.timeout.connect(self._on_schedule_tick)

    # ------------------------------------------------------------------
    # Worker wiring + scheduling
    # ------------------------------------------------------------------

    def start(self, worker: AcqWorker) -> None:
        self._worker = worker
        worker.frame_ready.connect(self.on_frame)
        worker.error.connect(self._on_worker_error)

        initial = self._pinned_mode or "fmcw"
        self._switch_mode(initial)
        worker.start()
        if not self._pinned_mode:
            self._arm_schedule()

    def _arm_schedule(self) -> None:
        cur = self._ctrl.mode
        dwell = self._args.dwell_fmcw if cur == "fmcw" else self._args.dwell_cw
        self._schedule_timer.start(int(max(0.5, dwell) * 1000))

    def _on_schedule_tick(self) -> None:
        if self._pinned_mode is not None:
            return
        nxt = "cw" if self._ctrl.mode == "fmcw" else "fmcw"
        self._switch_mode(nxt)
        self._arm_schedule()

    def _on_worker_error(self, message: str) -> None:
        self._status_lbl.setText(f"ERR: {message}")
        if "host unreachable" in message.lower() or "timed out" in message.lower():
            self._schedule_timer.stop()
            self._mode_lbl.setText("RX ERROR")
            self._mode_lbl.setStyleSheet(
                "font-size: 18px; font-weight: bold; "
                "background-color: #662222; color: #ffcccc; "
                "border-radius: 6px; padding: 6px;")

    def _switch_mode(self, mode: str) -> None:
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.pause()
        previous = self._ctrl.mode
        self._status_lbl.setText(
            f"Switching to {mode.upper()}"
            + (" (reopening hardware contexts)..." if self._args.reopen_on_switch and previous is not None and previous != mode else "...")
        )
        time.sleep(0.08)
        try:
            if self._args.reopen_on_switch and previous is not None and previous != mode:
                self._ctrl.reopen_for_mode(mode)
                if mode == "fmcw":
                    self._refresh_fmcw_axes()
                elif mode == "cw":
                    self._on_reset_cw()
            elif mode == "fmcw":
                self._ctrl.set_mode_fmcw_tdd()
                self._refresh_fmcw_axes()
            else:
                self._ctrl.set_mode_cw()
                if previous != "cw":
                    self._on_reset_cw()
        except Exception as exc:
            self._status_lbl.setText(f"Mode switch FAILED: {exc}")
            return
        time.sleep(0.15)
        if worker is not None:
            worker.resume()
        self._update_mode_indicator()

    def _refresh_fmcw_axes(self) -> None:
        a   = self._args
        spc = self._ctrl.spc
        if spc <= 0:
            return
        slope = a.chirp_bw / (a.ramp_time_us * 1e-6)
        freq = np.fft.fftshift(np.fft.fftfreq(spc, 1.0 / a.sample_rate))
        self._range_full = ((freq - a.signal_freq) * SPEED_OF_LIGHT / (2.0 * slope)).astype(np.float32)
        self._pos_mask = self._range_full >= 0.0
        self._range_axis = self._range_full[self._pos_mask]
        if self._range_axis.size == 0:
            return
        # Velocity axis (Doppler bins, FFT-shifted)
        nc      = int(a.num_chirps)
        pri_s   = a.ramp_time_us / 1e6 + a.frame_guard_ms / 1e3
        lam     = SPEED_OF_LIGHT / a.output_freq
        v_max   = lam / (4.0 * pri_s)
        if nc % 2 == 0:
            self._velocity_axis = np.linspace(-v_max, v_max - 2 * v_max / nc,
                                              nc, dtype=np.float32)
        else:
            self._velocity_axis = np.linspace(-v_max, v_max, nc, dtype=np.float32)

        # Update plot bounds for the RD heatmap and range profile
        max_r = float(self._range_axis[-1])
        x_max = float(min(self._args.max_display_range_m, max_r))
        self._p_rng.setXRange(0.0, x_max, padding=0)
        self._p_rng.setLimits(xMin=0.0, xMax=max_r)
        # Map the full image height to the full range axis; the view clips to x_max.
        self._rd_img.setRect(pg.QtCore.QRectF(
            float(-v_max), 0.0, float(2 * v_max), max_r))
        self._p_rd.setXRange(-v_max, v_max, padding=0)
        self._p_rd.setYRange(0.0, x_max, padding=0)
        self._p_rd.setLimits(xMin=-v_max, xMax=v_max, yMin=0.0, yMax=max_r)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)

        # Sidebar in a scroll area (lots of controls)
        sidebar = self._build_controls()
        scroll = QScrollArea()
        scroll.setWidget(sidebar)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(260)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(scroll, stretch=0)
        lay.addWidget(self._build_plots(), stretch=1)
        self.setCentralWidget(root)

    # ------------------------------------------------------------------
    # Controls panel
    # ------------------------------------------------------------------

    def _lbl(self, text, small=False):
        w = QLabel(text)
        w.setStyleSheet(f"font-size: {'10' if small else '11'}px; color: #ccc;")
        return w

    def _slider(self, lo, hi, val, tick=0):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        if tick:
            s.setTickInterval(tick)
            s.setTickPosition(QSlider.TicksBelow)
        return s

    def _build_controls(self) -> QWidget:
        outer = QWidget()
        lay = QVBoxLayout(outer)
        lay.setSpacing(6)
        lay.setContentsMargins(6, 6, 6, 6)

        # ── Active-mode indicator ─────────────────────────────────────
        self._mode_lbl = QLabel("─")
        self._mode_lbl.setAlignment(Qt.AlignCenter)
        self._mode_lbl.setStyleSheet(
            "font-size: 18px; font-weight: bold; "
            "background-color: #333355; color: #aaccff; "
            "border-radius: 6px; padding: 6px;")
        lay.addWidget(self._mode_lbl)

        # ── Schedule ─────────────────────────────────────────────────
        sched_box = QGroupBox("Schedule")
        sl = QVBoxLayout(sched_box)
        sl.setSpacing(2)
        sl.addWidget(self._lbl("Mode pin", True))
        self._pin_cb = QComboBox()
        self._pin_cb.addItems(["auto-cycle", "FMCW only", "CW only"])
        if self._args.pin == "fmcw": self._pin_cb.setCurrentIndex(1)
        elif self._args.pin == "cw": self._pin_cb.setCurrentIndex(2)
        self._pin_cb.currentIndexChanged.connect(self._on_pin_changed)
        sl.addWidget(self._pin_cb)

        sl.addWidget(self._lbl("FMCW dwell (s)", True))
        self._dwfm_sl  = self._slider(2, 30, int(self._args.dwell_fmcw), 5)
        self._dwfm_lbl = self._lbl(f"{int(self._args.dwell_fmcw)} s", True)
        def _dwfm(v):
            self._dwfm_lbl.setText(f"{v} s")
            self._args.dwell_fmcw = float(v)
        self._dwfm_sl.valueChanged.connect(_dwfm)
        sl.addWidget(self._dwfm_sl); sl.addWidget(self._dwfm_lbl)

        sl.addWidget(self._lbl("CW dwell (s)", True))
        self._dwcw_sl  = self._slider(2, 60, int(self._args.dwell_cw), 5)
        self._dwcw_lbl = self._lbl(f"{int(self._args.dwell_cw)} s", True)
        def _dwcw(v):
            self._dwcw_lbl.setText(f"{v} s")
            self._args.dwell_cw = float(v)
        self._dwcw_sl.valueChanged.connect(_dwcw)
        sl.addWidget(self._dwcw_sl); sl.addWidget(self._dwcw_lbl)
        lay.addWidget(sched_box)

        # ── Beam ─────────────────────────────────────────────────────
        beam_box = QGroupBox("Beam")
        bl = QVBoxLayout(beam_box); bl.setSpacing(2)
        bl.addWidget(self._lbl("Steering (°)", True))
        self._steer_sl  = self._slider(-80, 80, int(self._args.steer), 20)
        self._steer_lbl = self._lbl(f"{int(self._args.steer)}°", True)
        self._steer_sl.valueChanged.connect(self._on_steer)
        bl.addWidget(self._steer_sl); bl.addWidget(self._steer_lbl)
        lay.addWidget(beam_box)

        # ── RX / TX gains ───────────────────────────────────────────
        gn_box = QGroupBox("Gains")
        gl = QVBoxLayout(gn_box); gl.setSpacing(2)
        gl.addWidget(self._lbl("RX gain (dB)", True))
        self._rxgain_sl  = self._slider(-3, 70, self._args.rx_gain, 10)
        self._rxgain_lbl = self._lbl(f"{self._args.rx_gain} dB", True)
        self._rxgain_sl.valueChanged.connect(
            lambda v: (self._rxgain_lbl.setText(f"{v} dB"),
                       self._ctrl.set_rx_gain(v)))
        gl.addWidget(self._rxgain_sl); gl.addWidget(self._rxgain_lbl)

        gl.addWidget(self._lbl("TX gain (dB)", True))
        self._txgain_sl  = self._slider(-30, 0, self._args.tx_gain, 5)
        self._txgain_lbl = self._lbl(f"{self._args.tx_gain} dB", True)
        self._txgain_sl.valueChanged.connect(
            lambda v: (self._txgain_lbl.setText(f"{v} dB"),
                       self._ctrl.set_tx_gain(v)))
        gl.addWidget(self._txgain_sl); gl.addWidget(self._txgain_lbl)
        lay.addWidget(gn_box)

        # ── FMCW params (Mode A) ─────────────────────────────────────
        fmcw_box = QGroupBox("Mode A — FMCW")
        fl = QVBoxLayout(fmcw_box); fl.setSpacing(2)
        fl.addWidget(self._lbl("Bandwidth (MHz)", True))
        self._bw_sl  = self._slider(100, 500, int(self._args.chirp_bw / 1e6), 50)
        self._bw_lbl = self._lbl(f"{int(self._args.chirp_bw/1e6)} MHz", True)
        self._bw_sl.valueChanged.connect(
            lambda v: self._bw_lbl.setText(f"{v} MHz"))
        fl.addWidget(self._bw_sl); fl.addWidget(self._bw_lbl)

        fl.addWidget(self._lbl("Ramp time (µs)", True))
        self._ramp_sl  = self._slider(200, 1000, int(self._args.ramp_time_us), 100)
        self._ramp_lbl = self._lbl(f"{int(self._args.ramp_time_us)} µs", True)
        self._ramp_sl.valueChanged.connect(
            lambda v: self._ramp_lbl.setText(f"{v} µs"))
        fl.addWidget(self._ramp_sl); fl.addWidget(self._ramp_lbl)

        fl.addWidget(self._lbl("# chirps", True))
        self._nc_sl  = self._slider(16, 256, int(self._args.num_chirps), 32)
        self._nc_sl.setSingleStep(16)
        self._nc_lbl = self._lbl(f"{int(self._args.num_chirps)}", True)
        self._nc_sl.valueChanged.connect(
            lambda v: self._nc_lbl.setText(f"{v}"))
        fl.addWidget(self._nc_sl); fl.addWidget(self._nc_lbl)

        self._mti_cb = QCheckBox("MTI clutter removal")
        self._mti_cb.setChecked(bool(self._args.mti))
        self._mti_cb.stateChanged.connect(
            lambda s: setattr(self._args, "mti", s == Qt.Checked))
        fl.addWidget(self._mti_cb)

        self._win_cb = QCheckBox("Range/Doppler window")
        self._win_cb.setChecked(bool(self._args.window))
        self._win_cb.stateChanged.connect(
            lambda s: setattr(self._args, "window", s == Qt.Checked))
        fl.addWidget(self._win_cb)

        self._leak_cb = QCheckBox("Suppress TX leakage")
        self._leak_cb.setChecked(bool(self._args.suppress_leakage))
        self._leak_cb.stateChanged.connect(
            lambda s: setattr(self._args, "suppress_leakage", s == Qt.Checked))
        fl.addWidget(self._leak_cb)

        fl.addWidget(self._lbl("Zero blank bins", True))
        self._zrw_sl  = self._slider(0, 20, int(self._args.zero_blank_bins), 2)
        self._zrw_lbl = self._lbl(f"±{int(self._args.zero_blank_bins)} bins", True)
        def _zrw(v):
            self._args.zero_blank_bins = int(v)
            self._zrw_lbl.setText(f"±{v} bins")
        self._zrw_sl.valueChanged.connect(_zrw)
        fl.addWidget(self._zrw_sl); fl.addWidget(self._zrw_lbl)

        fl.addWidget(self._lbl("Min target range (cm)", True))
        self._minr_sl  = self._slider(0, 200, int(self._args.min_range_m * 100), 25)
        self._minr_lbl = self._lbl(f"{self._args.min_range_m:.2f} m", True)
        def _minr(v):
            self._args.min_range_m = float(v) / 100.0
            self._minr_lbl.setText(f"{self._args.min_range_m:.2f} m")
        self._minr_sl.valueChanged.connect(_minr)
        fl.addWidget(self._minr_sl); fl.addWidget(self._minr_lbl)

        fl.addWidget(self._lbl("Max display range (m)", True))
        self._maxr_sl  = self._slider(2, 30, int(self._args.max_display_range_m), 5)
        self._maxr_lbl = self._lbl(f"{int(self._args.max_display_range_m)} m", True)
        self._maxr_sl.valueChanged.connect(self._on_max_display_range)
        fl.addWidget(self._maxr_sl); fl.addWidget(self._maxr_lbl)

        ap_btn = QPushButton("Apply FMCW params")
        ap_btn.setFixedHeight(24)
        ap_btn.clicked.connect(self._on_apply_fmcw)
        fl.addWidget(ap_btn)
        lay.addWidget(fmcw_box)

        # ── Display levels ───────────────────────────────────────────
        dis_box = QGroupBox("RD heatmap levels")
        dl = QVBoxLayout(dis_box); dl.setSpacing(2)
        dl.addWidget(self._lbl("Floor (dB)", True))
        self._rd_floor_sl = self._slider(-60, 60, 0, 10)
        self._rd_floor_lbl = self._lbl("0 dB", True)
        self._rd_floor_sl.valueChanged.connect(
            lambda v: self._rd_floor_lbl.setText(f"{v} dB"))
        dl.addWidget(self._rd_floor_sl); dl.addWidget(self._rd_floor_lbl)
        dl.addWidget(self._lbl("Ceiling (dB)", True))
        self._rd_ceil_sl  = self._slider(-20, 100, 60, 10)
        self._rd_ceil_lbl = self._lbl("60 dB", True)
        self._rd_ceil_sl.valueChanged.connect(
            lambda v: self._rd_ceil_lbl.setText(f"{v} dB"))
        dl.addWidget(self._rd_ceil_sl); dl.addWidget(self._rd_ceil_lbl)
        auto_lvl_btn = QPushButton("Auto levels")
        auto_lvl_btn.setFixedHeight(24)
        auto_lvl_btn.clicked.connect(self._on_auto_rd_levels)
        dl.addWidget(auto_lvl_btn)
        lay.addWidget(dis_box)

        # ── Myo controls (Mode C) ────────────────────────────────────
        myo_box = QGroupBox("Mode C — Myo")
        ml = QVBoxLayout(myo_box); ml.setSpacing(2)
        ml.addWidget(self._lbl("Threshold (×10)", True))
        self._thr_sl  = self._slider(1, 10, int(self._args.myo_threshold * 10), 1)
        self._thr_lbl = self._lbl(f"{self._args.myo_threshold:.1f}", True)
        def _thr_changed(v):
            t = v / 10.0
            self._thr_lbl.setText(f"{t:.1f}")
            self._myo_proc.threshold = t
            self._myo_thr_line.setValue(t)
        self._thr_sl.valueChanged.connect(_thr_changed)
        ml.addWidget(self._thr_sl); ml.addWidget(self._thr_lbl)

        ml.addWidget(self._lbl("Band low (Hz)", True))
        self._myolo_sl  = self._slider(1, 200, int(self._args.myo_band_lo), 25)
        self._myolo_lbl = self._lbl(f"{self._args.myo_band_lo:.0f} Hz", True)
        self._myolo_sl.valueChanged.connect(self._on_myo_band_change)
        ml.addWidget(self._myolo_sl); ml.addWidget(self._myolo_lbl)

        ml.addWidget(self._lbl("Band high (Hz)", True))
        self._myohi_sl  = self._slider(50, 800, int(self._args.myo_band_hi), 50)
        self._myohi_lbl = self._lbl(f"{self._args.myo_band_hi:.0f} Hz", True)
        self._myohi_sl.valueChanged.connect(self._on_myo_band_change)
        ml.addWidget(self._myohi_sl); ml.addWidget(self._myohi_lbl)

        recal_btn = QPushButton("Recalibrate myo")
        recal_btn.setFixedHeight(24)
        recal_btn.clicked.connect(self._myo_proc.reset_calibration)
        ml.addWidget(recal_btn)
        lay.addWidget(myo_box)

        # ── Vitals (Mode B) ──────────────────────────────────────────
        vit_box = QGroupBox("Mode B — Vitals")
        vl = QVBoxLayout(vit_box); vl.setSpacing(2)
        vl.addWidget(self._lbl("Min RR confidence (×100)", True))
        self._rrconf_sl  = self._slider(0, 100, int(self._args.min_resp_conf * 100), 10)
        self._rrconf_lbl = self._lbl(f"{self._args.min_resp_conf:.2f}", True)
        def _rrconf(v):
            self._args.min_resp_conf = float(v) / 100.0
            self._rrconf_lbl.setText(f"{self._args.min_resp_conf:.2f}")
        self._rrconf_sl.valueChanged.connect(_rrconf)
        vl.addWidget(self._rrconf_sl); vl.addWidget(self._rrconf_lbl)

        vl.addWidget(self._lbl("Min HR confidence (×100)", True))
        self._hrconf_sl  = self._slider(0, 100, int(self._args.min_hr_conf * 100), 10)
        self._hrconf_lbl = self._lbl(f"{self._args.min_hr_conf:.2f}", True)
        def _hrconf(v):
            self._args.min_hr_conf = float(v) / 100.0
            self._hrconf_lbl.setText(f"{self._args.min_hr_conf:.2f}")
        self._hrconf_sl.valueChanged.connect(_hrconf)
        vl.addWidget(self._hrconf_sl); vl.addWidget(self._hrconf_lbl)

        reset_btn = QPushButton("Reset CW phase ring")
        reset_btn.setFixedHeight(24)
        reset_btn.clicked.connect(self._on_reset_cw)
        vl.addWidget(reset_btn)
        lay.addWidget(vit_box)

        # ── Recording ────────────────────────────────────────────────
        rec_box = QGroupBox("Recording")
        rl = QVBoxLayout(rec_box); rl.setSpacing(2)
        if not self._args.no_record:
            self._save_cb = QCheckBox("Record session")
            self._save_cb.setChecked(False)
            self._save_cb.stateChanged.connect(self._on_save_toggle)
            rl.addWidget(self._save_cb)
        self._save_lbl = self._lbl("Not recording", True)
        self._save_lbl.setStyleSheet("font-size: 10px; color: #888;")
        rl.addWidget(self._save_lbl)
        lay.addWidget(rec_box)

        # ── Status / quit ────────────────────────────────────────────
        self._status_lbl = QLabel("Connecting…")
        self._status_lbl.setStyleSheet("font-size: 10px; color: #88aaff;")
        self._status_lbl.setWordWrap(True)
        lay.addWidget(self._status_lbl)

        quit_btn = QPushButton("Quit")
        quit_btn.setStyleSheet(
            "background-color: #662222; color: white; font-weight: bold;")
        quit_btn.setFixedHeight(28)
        quit_btn.clicked.connect(self.close)
        lay.addWidget(quit_btn)

        lay.addStretch()
        return outer

    # ------------------------------------------------------------------
    # Plots panel (2x3 grid)
    # ------------------------------------------------------------------

    def _build_plots(self) -> pg.GraphicsLayoutWidget:
        glw = pg.GraphicsLayoutWidget()

        # ── (0,0) Mode A — Range-Doppler heatmap ─────────────────────
        p_rd = glw.addPlot(row=0, col=0, title="Mode A — Range-Doppler  (FMCW)")
        p_rd.setLabel("bottom", "Velocity [m/s]", **LABEL_KW)
        p_rd.setLabel("left",   "Range [m]",      **LABEL_KW)
        p_rd.enableAutoRange("xy", False)
        p_rd.showGrid(x=True, y=True, alpha=0.2)
        rd_img = pg.ImageItem()
        rd_img.setLookupTable(VIRIDIS_LUT)
        rd_img.setRect(pg.QtCore.QRectF(-1.0, 0.0, 2.0, 8.0))
        p_rd.addItem(rd_img)
        self._p_rd, self._rd_img = p_rd, rd_img

        # ── (0,1) Mode B — Vitals displacement ───────────────────────
        p_vit = glw.addPlot(row=0, col=1, title="Mode B — Vitals  (CW micro-Doppler)")
        p_vit.setLabel("bottom", "Time [s]",           **LABEL_KW)
        p_vit.setLabel("left",   "Displacement [mm]",  **LABEL_KW)
        p_vit.setXRange(0.0, self._args.cw_history_sec, padding=0)
        p_vit.enableAutoRange("x", False)
        p_vit.enableAutoRange("y", True)
        p_vit.showGrid(x=True, y=True, alpha=0.2)
        self._resp_curve = p_vit.plot(
            pen=pg.mkPen(color=(0, 255, 200), width=1.5), name="Resp")
        self._hr_curve   = p_vit.plot(
            pen=pg.mkPen(color=(255, 100, 200), width=1.5), name="HR")
        p_vit.addLegend(offset=(-10, 10))
        self._vit_text = pg.TextItem("RR: ––  HR: ––", color=(220, 240, 255), anchor=(0, 0))
        self._vit_text.setParentItem(p_vit.vb)
        self._vit_text.setPos(10, 10)
        self._p_vit = p_vit

        # ── (0,2) Mode C — Myo-index trace ────────────────────────────
        p_myo = glw.addPlot(row=0, col=2, title="Mode C — Myo-index  (EMG proxy)")
        p_myo.setLabel("bottom", "Time [s]",  **LABEL_KW)
        p_myo.setLabel("left",   "Myo-index", **LABEL_KW)
        p_myo.setXRange(-self._args.myo_history_sec, 0.0, padding=0)
        p_myo.setYRange(0.0, 1.0, padding=0)
        p_myo.enableAutoRange("xy", False)
        p_myo.setLimits(xMin=-self._args.myo_history_sec, xMax=0.0,
                        yMin=0.0, yMax=1.0)
        p_myo.showGrid(x=True, y=True, alpha=0.2)
        self._myo_thr_line = pg.InfiniteLine(
            pos=self._args.myo_threshold, angle=0, movable=False,
            pen=pg.mkPen(color=(255, 80, 80), width=1, style=Qt.DashLine))
        p_myo.addItem(self._myo_thr_line)
        self._myo_curve = p_myo.plot(
            pen=pg.mkPen(color=(255, 165, 0), width=2))
        self._myo_text = pg.TextItem("rest", color=(220, 240, 255), anchor=(0, 0))
        self._myo_text.setParentItem(p_myo.vb)
        self._myo_text.setPos(10, 10)
        self._p_myo = p_myo

        # ── (1,0) Mode A — Range profile + motion overlay ────────────
        p_rng = glw.addPlot(row=1, col=0, title="Mode A — Range profile  (mean of chirps)")
        p_rng.setLabel("bottom", "Range [m]",       **LABEL_KW)
        p_rng.setLabel("left",   "Magnitude [dB]",  **LABEL_KW)
        p_rng.setXRange(0.0, 8.0, padding=0)
        p_rng.setYRange(-20.0, 60.0, padding=0)
        p_rng.enableAutoRange("x", False)
        p_rng.enableAutoRange("y", True)
        p_rng.showGrid(x=True, y=True, alpha=0.2)
        self._rng_curve = p_rng.plot(pen=pg.mkPen(color=(120, 200, 255), width=1.5))
        self._tgt_line = pg.InfiniteLine(
            pos=0.0, angle=90, movable=False,
            pen=pg.mkPen(color=(255, 200, 80), width=1.5, style=Qt.DashLine))
        p_rng.addItem(self._tgt_line)
        self._rng_text = pg.TextItem(
            "presence: –   target: –   motion: –",
            color=(255, 220, 180), anchor=(0, 0))
        self._rng_text.setParentItem(p_rng.vb)
        self._rng_text.setPos(10, 10)
        self._p_rng = p_rng

        # ── (1,1) Mode B — Phase PSD 0–4 Hz ──────────────────────────
        p_psd = glw.addPlot(row=1, col=1, title="Mode B — Phase PSD  (0–4 Hz)")
        p_psd.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_psd.setLabel("left",   "PSD [dB/Hz]",    **LABEL_KW)
        p_psd.setXRange(0.0, 4.0, padding=0)
        p_psd.enableAutoRange("x", False)
        p_psd.enableAutoRange("y", True)
        p_psd.showGrid(x=True, y=True, alpha=0.2)
        p_psd.addItem(pg.LinearRegionItem(
            [0.15, 0.50], movable=False,
            brush=pg.mkBrush(0, 255, 200, 30),
            pen=pg.mkPen(color=(0, 255, 200), width=0.5)))
        p_psd.addItem(pg.LinearRegionItem(
            [0.80, 2.50], movable=False,
            brush=pg.mkBrush(255, 100, 200, 30),
            pen=pg.mkPen(color=(255, 100, 200), width=0.5)))
        self._psd_curve = p_psd.plot(
            pen=pg.mkPen(color=(255, 200, 50), width=1.5))
        self._resp_bpm_line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(color=(0, 255, 200), width=2))
        self._hr_bpm_line   = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(color=(255, 100, 200), width=2))
        p_psd.addItem(self._resp_bpm_line)
        p_psd.addItem(self._hr_bpm_line)
        self._p_psd = p_psd

        # ── (1,2) Mode C — Amplitude PSD 0–600 Hz ────────────────────
        p_amp = glw.addPlot(row=1, col=2, title="Mode C — Amplitude PSD  (EMG band)")
        p_amp.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_amp.setLabel("left",   "PSD [dB]",       **LABEL_KW)
        p_amp.setXRange(0.0, 600.0, padding=0)
        p_amp.enableAutoRange("x", False)
        p_amp.enableAutoRange("y", True)
        p_amp.showGrid(x=True, y=True, alpha=0.2)
        self._myo_band_region = pg.LinearRegionItem(
            [self._args.myo_band_lo, self._args.myo_band_hi], movable=False,
            brush=pg.mkBrush(255, 165, 0, 30),
            pen=pg.mkPen(color=(255, 165, 0), width=0.5))
        p_amp.addItem(self._myo_band_region)
        self._amp_curve = p_amp.plot(
            pen=pg.mkPen(color=(255, 165, 0), width=1.5))
        self._p_amp = p_amp

        # Even row stretch
        for i in range(2): glw.ci.layout.setRowStretchFactor(i, 1)
        for i in range(3): glw.ci.layout.setColumnStretchFactor(i, 1)
        return glw

    def _update_mode_indicator(self) -> None:
        m = self._ctrl.mode
        if m == "fmcw":
            self._mode_lbl.setText("●  FMCW (A)")
            self._mode_lbl.setStyleSheet(
                "font-size: 18px; font-weight: bold; "
                "background-color: #224466; color: #aaeeff; "
                "border-radius: 6px; padding: 6px;")
        elif m == "cw":
            self._mode_lbl.setText("●  CW (B + C)")
            self._mode_lbl.setStyleSheet(
                "font-size: 18px; font-weight: bold; "
                "background-color: #444422; color: #ffeeaa; "
                "border-radius: 6px; padding: 6px;")
        else:
            self._mode_lbl.setText("─")

    # ------------------------------------------------------------------
    # Frame routing
    # ------------------------------------------------------------------

    def on_frame(self, raw_iq: np.ndarray, mode: str) -> None:
        t0 = time.monotonic()
        self._frame_count += 1
        if self._saving:
            self._rec_iq.append((t0, raw_iq.copy(), mode))

        if mode == "fmcw":
            self._on_fmcw_frame(raw_iq, t0)
        elif mode == "cw":
            self._on_cw_frame(raw_iq, t0)

        dt_ms = (time.monotonic() - t0) * 1000
        self._status_lbl.setText(
            f"frame {self._frame_count}  mode {mode.upper()}  "
            f"proc {dt_ms:.0f} ms\nsaved {len(self._rec_iq)}"
        )

    # ------------------------------------------------------------------
    # Mode A — TDD FMCW Range-Doppler
    # ------------------------------------------------------------------

    def _on_fmcw_frame(self, raw: np.ndarray, t0: float) -> None:
        nc = int(self._args.num_chirps)
        if self._range_axis is None or self._velocity_axis is None or self._pos_mask is None:
            return
        if raw.size < self._ctrl.spc:
            return

        # Slice into chirp matrix
        cm = self._ctrl.slice_chirp_matrix(raw)
        spc = cm.shape[1]
        if spc < 8 or cm.shape[0] != nc:
            return

        # Range FFT per chirp. Keep the shifted full IF spectrum, then select
        # positive physical ranges after subtracting the 100 kHz IF tone.
        # Normalise by window coherent gain so dB values are per-sample amplitude
        win_r  = (np.blackman(spc) if self._args.window else np.ones(spc)).astype(np.complex64)
        norm_r = float(np.sum(np.abs(win_r))) + 1e-30
        rng_fft_full = np.fft.fftshift(
            np.fft.fft(cm * win_r[None, :], axis=1), axes=1
        ) / norm_r

        # MTI clutter removal (subtract chirp-mean)
        if self._args.mti:
            rng_fft_mti = rng_fft_full - rng_fft_full.mean(axis=0, keepdims=True)
        else:
            rng_fft_mti = rng_fft_full

        # Doppler FFT across chirps — normalise by window sum
        win_d  = (np.blackman(nc) if self._args.window else np.ones(nc)).astype(np.complex64)
        norm_d = float(np.sum(np.abs(win_d))) + 1e-30
        rd_full = np.fft.fftshift(
            np.fft.fft(rng_fft_mti * win_d[:, None], axis=0), axes=0) / norm_d
        rd_full = rd_full[:, self._pos_mask]
        rd_mag = np.abs(rd_full)
        rd_db  = (20.0 * np.log10(rd_mag + 1e-12)).astype(np.float32)

        # Range profile = mean magnitude across chirps (no MTI for clean view)
        rp_full = np.abs(rng_fft_full).mean(axis=0)
        rp_db = (20.0 * np.log10(rp_full[self._pos_mask] + 1e-12)).astype(np.float32)

        if self._args.suppress_leakage and rd_db.shape[1] > 0:
            zrw = int(max(0, self._args.zero_blank_bins))
            if zrw > 0:
                blank = min(zrw, rd_db.shape[1])
                rd_db = rd_db.copy()
                rp_db = rp_db.copy()
                rd_floor = float(np.nanmin(rd_db))
                rp_floor = float(np.nanmin(rp_db))
                rd_db[:, :blank] = rd_floor
                rp_db[:blank] = rp_floor

        # Cache for auto-level button; auto-level on very first frame.
        self._last_rd_db = rd_db
        if not self._rd_auto_leveled:
            self._rd_auto_leveled = True
            p5  = float(np.percentile(rd_db, 5))
            p95 = float(np.percentile(rd_db, 95))
            span = max(10.0, p95 - p5)
            fl   = int(np.clip(p5 - 2.0, -60, 60))
            cl   = int(np.clip(p5 + span * 1.1, fl + 10, 100))
            self._rd_floor_sl.setValue(fl)
            self._rd_ceil_sl.setValue(cl)

        # Plot RD heatmap — rd_db shape is (nc, spc//2) = (Doppler, Range)
        # With row-major imageAxisOrder: setImage(data[row=y, col=x])
        # Rows → range axis (y), cols → Doppler axis (x)  →  rd_db.T
        self._rd_img.setImage(
            rd_db.T,
            levels=(float(self._rd_floor_sl.value()),
                    float(self._rd_ceil_sl.value())),
            autoLevels=False,
        )

        # Range profile line
        L = min(len(self._range_axis), len(rp_db))
        x = self._range_axis[:L]
        y = rp_db[:L]
        self._rng_curve.setData(x=x, y=y)

        # Target = peak in range profile excluding configured near-field leakage.
        range_per_bin = float(x[1] - x[0]) if L > 1 else 1.0
        leak_bins = max(2, int(self._args.min_range_m / max(range_per_bin, 1e-6)))
        if L > leak_bins + 2:
            search_mask = np.zeros(L, dtype=bool)
            search_mask[leak_bins:] = True
            search_mask &= x <= float(self._args.max_display_range_m)
            candidates = np.flatnonzero(search_mask)
            if candidates.size == 0:
                candidates = np.arange(leak_bins, L)
            tgt_idx = int(candidates[np.argmax(y[candidates])])
            tgt_m   = float(x[tgt_idx])
            tgt_db  = float(y[tgt_idx])
        else:
            tgt_idx, tgt_m, tgt_db = 0, 0.0, 0.0
        self._tgt_line.setValue(tgt_m)
        self._last_target_m = tgt_m

        # Motion energy: relative ratio vs median RD power (scale-invariant)
        zero_bin = nc // 2
        motion_band = list(range(0, max(0, zero_bin - 1))) + \
                      list(range(min(nc, zero_bin + 2), nc))
        rd_mag_sq  = rd_mag ** 2
        noise_med  = float(np.median(rd_mag_sq)) + 1e-30
        if motion_band:
            motion_rel = float(np.mean(rd_mag_sq[motion_band, :])) / noise_med
        else:
            motion_rel = 1.0
        # motion_n ≈ 0 dB at rest (motion-band ≈ median); rises with activity
        motion_n = float(10.0 * np.log10(motion_rel + 1e-15))
        self._motion_history.append(motion_n)
        self._last_motion = motion_n

        # Activity heuristic (no ML) — thresholds are relative-dB above noise median
        if motion_n > 6.0:
            verdict = "MOVING";        color = "#ff8866"
        elif motion_n > 2.0:
            verdict = "small motion";  color = "#aaccff"
        else:
            verdict = "still";         color = "#88aa88"

        # Presence: peak SNR vs noise floor
        noise_floor = float(np.median(y))
        snr_db = tgt_db - noise_floor
        presence = "yes" if snr_db > 6.0 else "no"

        self._rng_text.setHtml(
            f'<span style="color:{color}; font-size:13pt; font-weight:bold;">'
            f'{verdict}</span>'
            f'<br><span style="color:#cccccc; font-size:10pt;">'
            f'presence: {presence}   target ≈ {tgt_m:.2f} m   '
            f'SNR={snr_db:.1f} dB</span>'
        )

    # ------------------------------------------------------------------
    # Mode B + C — CW (parallel DSPs)
    # ------------------------------------------------------------------

    def _on_cw_frame(self, raw_iq: np.ndarray, t0: float) -> None:
        self._cw_proc.push(raw_iq)
        self._myo_proc.push(raw_iq)

        self._cw_compute_div += 1
        if self._cw_compute_div >= 10:
            self._cw_compute_div = 0
            r = self._cw_proc.compute()
            if r is not None:
                self._last_vitals = r
                self._update_vitals_panel(r)

        self._myo_compute_div += 1
        if self._myo_compute_div >= 4:
            self._myo_compute_div = 0
            mr = self._myo_proc.compute()
            if mr is not None:
                self._last_myo = mr
                self._update_myo_panel(mr)

    def _update_vitals_panel(self, r: VitalsResult) -> None:
        if len(r.resp_disp_mm) > 1:
            self._resp_curve.setData(x=r.time_axis, y=r.resp_disp_mm)
            self._hr_curve.setData(  x=r.time_axis, y=r.hr_disp_mm)
        if len(r.psd_freq_hz) > 1:
            self._psd_curve.setData(x=r.psd_freq_hz, y=r.psd_db)
        if r.resp_bpm > 0:
            self._resp_bpm_line.setValue(r.resp_bpm / 60.0)
        if r.hr_bpm > 0:
            self._hr_bpm_line.setValue(r.hr_bpm / 60.0)

        rr = f"{r.resp_bpm:5.1f}" if r.resp_conf > self._args.min_resp_conf else "  ––"
        hr = f"{r.hr_bpm:5.1f}"   if r.hr_conf   > self._args.min_hr_conf else "  ––"
        self._vit_text.setHtml(
            f'<span style="color:#00ffcc; font-size:14pt; font-weight:bold;">'
            f'RR {rr} bpm</span>'
            f'  <span style="color:#888888; font-size:10pt;">conf {r.resp_conf:.2f}</span>'
            f'<br><span style="color:#ff66cc; font-size:14pt; font-weight:bold;">'
            f'HR {hr} bpm</span>'
            f'  <span style="color:#888888; font-size:10pt;">conf {r.hr_conf:.2f}</span>'
        )

    def _update_myo_panel(self, mr: MyoResult) -> None:
        self._myo_history.append(mr.myo_index)
        self._myo_curve.setData(
            x=self._myo_t,
            y=np.array(self._myo_history, dtype=np.float32))
        if len(mr.spec_freq_hz) > 1:
            self._amp_curve.setData(x=mr.spec_freq_hz, y=mr.spec_db)

        if not mr.calibrated:
            pct = int(mr.cal_progress * 100)
            self._myo_text.setHtml(
                f'<span style="color:#ffaa44; font-size:12pt;">'
                f'calibrating… {pct}%</span>')
            return
        if mr.activation:
            self._myo_text.setHtml(
                f'<span style="color:#ff9999; font-size:14pt; font-weight:bold;">'
                f'ACTIVE</span>  '
                f'<span style="color:#cccccc; font-size:10pt;">'
                f'index {mr.myo_index:.2f}   centroid {mr.cur_centroid_hz:.0f} Hz</span>')
        else:
            self._myo_text.setHtml(
                f'<span style="color:#aaffaa; font-size:14pt; font-weight:bold;">'
                f'rest</span>  '
                f'<span style="color:#cccccc; font-size:10pt;">'
                f'index {mr.myo_index:.2f}   centroid {mr.cur_centroid_hz:.0f} Hz</span>')

    # ------------------------------------------------------------------
    # Control callbacks
    # ------------------------------------------------------------------

    def _on_max_display_range(self, value: int) -> None:
        self._args.max_display_range_m = float(value)
        self._maxr_lbl.setText(f"{value} m")
        if self._range_axis is not None:
            self._refresh_fmcw_axes()

    def _on_myo_band_change(self, _value: int) -> None:
        lo = float(self._myolo_sl.value())
        hi = float(self._myohi_sl.value())
        if hi <= lo + 5.0:
            hi = lo + 5.0
            self._myohi_sl.setValue(int(hi))
        self._args.myo_band_lo = lo
        self._args.myo_band_hi = hi
        self._myo_proc.band_lo = lo
        self._myo_proc.band_hi = hi
        self._myolo_lbl.setText(f"{lo:.0f} Hz")
        self._myohi_lbl.setText(f"{hi:.0f} Hz")
        self._myo_band_region.setRegion([lo, hi])

    def _on_auto_rd_levels(self) -> None:
        """Set floor/ceiling sliders from current RD frame statistics."""
        if self._last_rd_db is None:
            return
        p5  = float(np.percentile(self._last_rd_db, 5))
        p95 = float(np.percentile(self._last_rd_db, 95))
        span = max(10.0, p95 - p5)
        fl   = int(np.clip(p5 - 2.0, -60, 60))
        cl   = int(np.clip(p5 + span * 1.1, fl + 10, 100))
        self._rd_floor_sl.setValue(fl)
        self._rd_ceil_sl.setValue(cl)

    def _on_pin_changed(self, idx: int) -> None:
        mapping = {0: None, 1: "fmcw", 2: "cw"}
        self._pinned_mode = mapping.get(idx)
        if self._pinned_mode is None:
            self._arm_schedule()
        else:
            self._schedule_timer.stop()
            if self._ctrl.mode != self._pinned_mode:
                self._switch_mode(self._pinned_mode)

    def _on_steer(self, value: int) -> None:
        self._steer_lbl.setText(f"{value}°")
        self._ctrl.set_steer(float(value))

    def _on_apply_fmcw(self) -> None:
        a = self._args
        a.chirp_bw     = float(self._bw_sl.value()) * 1e6
        a.ramp_time_us = float(self._ramp_sl.value())
        # Snap to a multiple of 16
        nc = int(self._nc_sl.value())
        nc = max(16, (nc // 16) * 16)
        a.num_chirps   = nc
        self._nc_sl.setValue(nc)
        self._rd_auto_leveled = False
        # Re-program if currently in FMCW; otherwise will apply on next switch
        if self._ctrl.mode == "fmcw":
            self._switch_mode("fmcw")
        self._status_lbl.setText(
            f"FMCW: BW={a.chirp_bw/1e6:.0f} MHz  ramp={a.ramp_time_us:.0f}µs  "
            f"chirps={a.num_chirps}"
        )

    def _on_reset_cw(self) -> None:
        a = self._args
        self._cw_proc = CWProcessor(
            fs           = a.cw_sample_rate,
            signal_freq  = a.signal_freq,
            rf_freq      = a.output_freq,
            history_sec  = a.cw_history_sec,
            phase_fs     = a.cw_phase_fs,
            warmup_sec   = a.vitals_warmup_sec,
        )
        self._last_vitals = None

    def _on_save_toggle(self, state: int) -> None:
        self._saving = (state == Qt.Checked)
        if self._saving:
            self._rec_iq.clear()
            self._save_lbl.setText("● Recording…")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #ff8844;")
        else:
            self._flush_recording()

    def _flush_recording(self) -> None:
        if not self._rec_iq:
            self._save_lbl.setText("Not recording")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #888;")
            return
        try:
            base = Path(self._args.save_dir)
            base.mkdir(parents=True, exist_ok=True)
            sdir = base / datetime.now().strftime("%Y%m%d_%H%M%S_sentinel")
            sdir.mkdir()

            cfg = vars(self._args).copy()
            cfg["save_dir"] = str(cfg["save_dir"])
            (sdir / "config.json").write_text(
                json.dumps(cfg, indent=2, default=str), encoding="utf-8")

            ts   = np.array([t for t, _, _ in self._rec_iq], dtype=np.float64)
            mode = np.array([m for _, _, m in self._rec_iq])
            np.save(sdir / "timestamps.npy", ts)
            np.save(sdir / "modes.npy", mode)

            cw_frames   = [iq for _, iq, m in self._rec_iq if m == "cw"]
            fmcw_frames = [iq for _, iq, m in self._rec_iq if m == "fmcw"]
            if cw_frames:
                np.save(sdir / "cw_iq.npy",
                        np.stack(cw_frames, axis=0).astype(np.complex64))
            if fmcw_frames:
                # FMCW frames may share a length (rx_buffer_size). Stack if so;
                # otherwise save per-frame to avoid losing partial bursts.
                lens = {f.size for f in fmcw_frames}
                if len(lens) == 1:
                    np.save(sdir / "fmcw_iq.npy",
                            np.stack(fmcw_frames, axis=0).astype(np.complex64))
                else:
                    fdir = sdir / "fmcw_frames"
                    fdir.mkdir()
                    for i, f in enumerate(fmcw_frames):
                        np.save(fdir / f"frame_{i:06d}.npy", f)

            n = len(self._rec_iq)
            print(f"[sentinel] Saved {n} frames → {sdir}")
            self._save_lbl.setText(f"Saved {n} frames")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #88ff88;")
        except Exception as exc:
            print(f"[sentinel] Save failed: {exc}")
            self._save_lbl.setText("Save FAILED")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #ff4444;")

    def closeEvent(self, event) -> None:
        self._schedule_timer.stop()
        if hasattr(self, "_worker"):
            self._worker.requestInterruption()
            self._worker.resume()
            self._worker.wait(3000)
        self._ctrl.teardown()
        event.accept()


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

def _dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal   = QPalette()
    dark  = QColor(30, 30, 46)
    mid   = QColor(50, 50, 70)
    light = QColor(220, 220, 220)
    acc   = QColor(100, 150, 255)
    pal.setColor(QPalette.Window,          dark)
    pal.setColor(QPalette.WindowText,      light)
    pal.setColor(QPalette.Base,            QColor(20, 20, 35))
    pal.setColor(QPalette.AlternateBase,   mid)
    pal.setColor(QPalette.ToolTipBase,     mid)
    pal.setColor(QPalette.ToolTipText,     light)
    pal.setColor(QPalette.Text,            light)
    pal.setColor(QPalette.Button,          mid)
    pal.setColor(QPalette.ButtonText,      light)
    pal.setColor(QPalette.Highlight,       acc)
    pal.setColor(QPalette.HighlightedText, dark)
    app.setPalette(pal)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    args = build_parser().parse_args(argv)

    ctrl = HardwareController(args)
    ctrl.connect(target_mode=args.pin or "fmcw")
    if args.steer != 0.0:
        ctrl.set_steer(args.steer)

    print(
        f"[sentinel] connected   "
        f"RF={args.output_freq/1e9:.4f} GHz   "
        f"A_SR={args.sample_rate/1e3:.0f} kHz   CW_SR={args.cw_sample_rate/1e3:.0f} kHz   "
        f"RX={args.rx_gain} dB   TX={args.tx_gain} dB   "
        f"FMCW: BW={args.chirp_bw/1e6:.0f} MHz  ramp={args.ramp_time_us:.0f}µs  "
        f"chirps={args.num_chirps}   "
        f"schedule: FMCW {args.dwell_fmcw:.1f}s ↔ CW {args.dwell_cw:.1f}s"
        + (f"   (PINNED to {args.pin.upper()})" if args.pin else "")
    )

    app = QApplication(sys.argv)
    _dark_palette(app)

    win    = SentinelDashboard(ctrl, args)
    worker = AcqWorker(ctrl)
    win.start(worker)

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
