#!/usr/bin/env python3
"""SENTINEL Mode A v5 — Windows-native, TDD soft-sync, PyQt5/pyqtgraph display.

Architecture
------------
  radar_backends.py   hardware bring-up, TDD/continuous backends
  radar_dsp.py        2D FFT, MTI, CFAR, range/velocity axes
  sentinel_mode_a_v5  PyQt5 GUI + QThread acquisition + this CLI

Proven topology (Windows, 2026-04)
-----------------------------------
  PC → Pluto  via  ip:192.168.2.1  (RX data + TDD/GPIO control)
  PC → Phaser via  ip:phaser.local  (ADF4159 PLL + ADAR1000 beamformer)

  Do NOT use ip:phaser.local:50901 or WSL-forwarded paths — both cause
  ETIMEDOUT on s.rx() regardless of the acquisition mode.

Default tuning — spec-justified
---------------------------------
  --acq tdd  --tdd-trigger soft
  --bw 500e6  --ramp-time-us 300  --num-chirps 256  --sample-rate 4e6

  BW = 500 MHz (spec says 200 MHz, but spec was written for WSL+continuous where
  buffer throughput was the bottleneck).  TDD captures exactly num_chirps×spc
  samples regardless of BW.  500 MHz = 0.30 m range resolution, which is strictly
  better for separating torso from limbs and for AoA estimation.

  Ramp = 300 µs (spec says 200 µs, but spec was written for continuous-mode PRF).
  300 µs is the proven TDD path.  Max velocity = ±24.4 m/s, sufficient for all
  human motion and falling.

  num_chirps = 256 (matches spec).  At 64 chirps velocity resolution = 0.76 m/s,
  too coarse to distinguish slow limb motion from zero-Doppler — a fatal flaw for
  micro-Doppler HAR.  256 chirps → 0.19 m/s resolution, which RadMamba requires.
  CPI = 76.8 ms, expected frame rate ~6-7 FPS with TDD soft-sync.

  RX gain = 45 dB (spec says 60 dB, but spec was written before recording data).
  At 70 dB: 3.3% of samples clip at ±4096 ADC counts, generating clipping harmonics
  at 36m / 72m that dominate the Range-Doppler map post-MTI.  Simulation confirms
  -20 dB from 70 dB (= 50 dB) eliminates clipping.  45 dB is the conservative safe
  starting point.  Try 50-55 dB if near-range targets are weak.

Run
---
  python sentinel_mode_a_v5.py
  python sentinel_mode_a_v5.py --acq continuous --sdr-uri usb:1.5.5
  python sentinel_mode_a_v5.py --num-chirps 128 --min-range-m 0.3
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from radar_dsp import MicroDopplerHistory, process_frame, save_session
from radar_backends import CaptureResult, HardwareParams, make_backend
from target_detection_dbfs import cfar as _cfar

# ---------------------------------------------------------------------------
# pyqtgraph global config
# ---------------------------------------------------------------------------
pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
pg.setConfigOption("background", "#1a1a2e")
pg.setConfigOption("foreground", "#dddddd")

CHEBYSHEV_TAPER = (8, 34, 84, 127, 127, 84, 34, 8)

def _lut(name: str) -> np.ndarray:
    try:
        cmap = pg.colormap.get(name, source="matplotlib")
    except Exception:
        cmap = pg.colormap.get(name)
    return cmap.getLookupTable(0.0, 1.0, 256)

INFERNO_LUT = _lut("inferno")
VIRIDIS_LUT = _lut("viridis")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel_mode_a_v5",
        description="SENTINEL Mode A v5: TDD soft-sync, PyQt5/pyqtgraph display",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--acq", choices=["tdd", "continuous"], default="tdd",
                   help="Acquisition backend")

    t = p.add_argument_group("radar tuning")
    t.add_argument("--bw", type=float, default=500e6, metavar="Hz",
                   help="Chirp bandwidth")
    t.add_argument("--ramp-time-us", type=int, default=300, metavar="us",
                   help="Ramp time in microseconds (300 is proven for TDD soft-sync)")
    t.add_argument("--num-chirps", type=int, default=256,
                   help="Chirps per frame. 256 gives 0.19 m/s Doppler resolution "
                        "needed for micro-Doppler HAR (spec requirement).")
    t.add_argument("--sample-rate", type=float, default=4e6, metavar="Hz")
    t.add_argument("--rx-gain", type=int, default=45,
                   help="RX gain dB (-3 to 70). 45 dB avoids ADC clipping from TX leakage.")
    t.add_argument("--tx-gain", type=int, default=0,
                   help="TX gain dB (0 = max power)")
    t.add_argument("--output-freq", type=float, default=10.25e9, metavar="Hz")
    t.add_argument("--center-freq", type=float, default=2.1e9, metavar="Hz")
    t.add_argument("--signal-freq", type=float, default=100e3, metavar="Hz",
                   help="IF tone frequency")
    t.add_argument("--gain-taper",
                   default=",".join(str(g) for g in CHEBYSHEV_TAPER),
                   metavar="g0,..,g7",
                   help="8 ADAR1000 channel gains (0-127). "
                        "Chebyshev default gives -30 dB sidelobes.")
    t.add_argument("--steer", type=float, default=0.0, metavar="DEG",
                   help="Initial beam steering angle")

    d = p.add_argument_group("DSP / display")
    d.add_argument("--min-range-m", type=float, default=0.5, metavar="M",
                   help="Minimum range for auto gate (metres). "
                        "Set lower than your closest expected target.")
    d.add_argument("--max-range", type=float, default=10.0, metavar="m",
                   help="Initial display range axis upper limit (metres). "
                        "Adjustable live with the slider.")
    d.add_argument("--history-len", type=int, default=64,
                   help="Micro-Doppler history depth in frames")

    r = p.add_argument_group("recording")
    r.add_argument("--save-dir", type=Path, default=Path("recordings"))

    hw = p.add_argument_group("hardware URIs")
    hw.add_argument("--rpi-uri", default="ip:phaser.local",
                    help="Phaser/Pi IIO URI")
    hw.add_argument("--sdr-uri", default="ip:192.168.2.1",
                    help="Pluto IIO URI.  Windows proven paths: "
                         "ip:192.168.2.1 (network) or usb:1.5.5 (USB).  "
                         "Do NOT use ip:phaser.local:50901 (ETIMEDOUT).")
    hw.add_argument("--tdd-uri", default=None,
                    help="Pluto TDD/GPIO URI (defaults to --sdr-uri)")

    tdd = p.add_argument_group("TDD options  (--acq tdd only)")
    tdd.add_argument("--tdd-trigger", choices=["soft", "internal", "external"],
                     default="soft",
                     help="TDD sync source.  'soft' is the proven working path. "
                          "'external' requires HDL/device-tree changes not present "
                          "in the current Pluto firmware.")
    tdd.add_argument("--frame-guard-ms", type=float, default=0.2,
                     help="Guard interval added to ramp to form PRI")
    tdd.add_argument("--tdd-arm-delay-s", type=float, default=0.05,
                     help="Seconds to wait after starting rx() before issuing sync. "
                          "0.05 s is enough over ip:192.168.2.1 (local network).")
    tdd.add_argument("--tdd-rx-timeout-ms", type=int, default=30000)

    return p


# ---------------------------------------------------------------------------
# Acquisition worker — QThread so rx() never blocks the GUI event loop
# ---------------------------------------------------------------------------

class RxWorker(QThread):
    data_ready = pyqtSignal(object)   # emits CaptureResult
    error      = pyqtSignal(str)

    def __init__(self, backend) -> None:
        super().__init__()
        self._backend = backend

    def run(self) -> None:
        # Flush stale DMA buffers that accumulate during connect() on continuous
        if getattr(self._backend, "name", None) == "continuous":
            for _ in range(3):
                try:
                    self._backend.capture()
                except Exception:
                    pass
        while not self.isInterruptionRequested():
            try:
                cap = self._backend.capture()
                self.data_ready.emit(cap)
            except Exception as exc:
                self.error.emit(str(exc))
                break


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SentinelWindow(QMainWindow):

    def __init__(self, backend, cfg, args: argparse.Namespace) -> None:
        super().__init__()
        self._backend = backend
        self._cfg     = cfg
        self._args    = args

        # Full range axis (includes negative-range mirror half)
        self._range_m   = cfg.range_axis()
        self._vel_ms    = cfg.velocity_axis()

        # Positive-range mask — only these bins are shown
        self._pos_mask  = self._range_m >= 0.0
        self._range_pos = self._range_m[self._pos_mask]

        self._last_rd: np.ndarray | None = None

        self._history = MicroDopplerHistory(
            history_len=args.history_len,
            doppler_bins=cfg.num_chirps,
        )

        self._saving         = False
        self._raw_bursts:    list[np.ndarray] = []
        self._raw_ch0:       list[np.ndarray] = []
        self._raw_ch1:       list[np.ndarray] = []
        self._spectrograms:  list[np.ndarray] = []
        self._valid_chirps:  list[int]        = []
        self._timestamps:    list[float]      = []

        self.setWindowTitle(
            f"SENTINEL Mode A v5  [{args.acq}/{args.tdd_trigger}]  "
            f"BW={cfg.chirp_bw/1e6:.0f} MHz  "
            f"{int(cfg.ramp_time_s * 1e6)} µs  "
            f"{cfg.num_chirps} chirps  "
            f"R_res={cfg.range_res_m:.2f} m  "
            f"v_res={cfg.vel_res_m_s:.2f} m/s"
        )
        self._build_ui()
        self.showMaximized()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        lay  = QHBoxLayout(root)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
        lay.addWidget(self._build_controls(), stretch=0)
        lay.addWidget(self._build_plots(),    stretch=1)
        self.setCentralWidget(root)

    def _build_controls(self) -> QWidget:
        box = QGroupBox("SENTINEL Controls")
        box.setFixedWidth(250)
        lay = QVBoxLayout(box)
        lay.setSpacing(4)

        def lbl(text: str, small: bool = False) -> QLabel:
            w = QLabel(text)
            w.setStyleSheet(f"font-size: {'10' if small else '11'}px; color: #ccc;")
            return w

        def slider(lo: int, hi: int, val: int, tick: int = 0) -> QSlider:
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(val)
            if tick:
                s.setTickInterval(tick)
                s.setTickPosition(QSlider.TicksBelow)
            return s

        # Beam steering
        lay.addWidget(lbl("Beam Steering (°)"))
        self._steer_sl  = slider(-80, 80, int(self._args.steer), 20)
        self._steer_lbl = lbl(f"{int(self._args.steer)}°", True)
        self._steer_sl.valueChanged.connect(self._on_steer)
        lay.addWidget(self._steer_sl)
        lay.addWidget(self._steer_lbl)

        # RX gain
        lay.addWidget(lbl("RX Gain (dB)"))
        self._rxgain_sl  = slider(-3, 70, self._args.rx_gain, 10)
        self._rxgain_lbl = lbl(f"{self._args.rx_gain} dB", True)
        self._rxgain_sl.valueChanged.connect(
            lambda v: self._rxgain_lbl.setText(f"{v} dB"))
        apply_gain = QPushButton("Apply RX Gain")
        apply_gain.setFixedHeight(24)
        apply_gain.clicked.connect(self._on_rx_gain_apply)
        lay.addWidget(self._rxgain_sl)
        lay.addWidget(self._rxgain_lbl)
        lay.addWidget(apply_gain)

        # Max display range
        lay.addWidget(lbl("Max Display Range (m)"))
        r_init = max(2, min(100, int(self._args.max_range)))
        self._maxr_sl  = slider(2, 100, r_init, 10)
        self._maxr_lbl = lbl(f"{r_init} m", True)
        self._maxr_sl.valueChanged.connect(self._on_max_range)
        lay.addWidget(self._maxr_sl)
        lay.addWidget(self._maxr_lbl)

        # DSP toggles
        self._mti_cb  = QCheckBox("MTI clutter filter")
        self._mti_cb.setChecked(True)
        self._win_cb  = QCheckBox("Hanning window")
        self._win_cb.setChecked(True)
        self._cfar_cb = QCheckBox("CFAR range gate")
        self._cfar_cb.setChecked(True)
        self._zrs_cb  = QCheckBox("Suppress TX leakage")
        self._zrs_cb.setChecked(True)
        for cb in (self._mti_cb, self._win_cb, self._cfar_cb, self._zrs_cb):
            lay.addWidget(cb)

        # CFAR bias
        lay.addWidget(lbl("CFAR bias (dB)"))
        self._cfar_sl  = slider(0, 50, 15)
        self._cfar_lbl = lbl("15 dB", True)
        self._cfar_sl.valueChanged.connect(
            lambda v: self._cfar_lbl.setText(f"{v} dB"))
        lay.addWidget(self._cfar_sl)
        lay.addWidget(self._cfar_lbl)

        # Zero-range blanking
        lay.addWidget(lbl("Zero-range blank (bins ±)"))
        self._zrw_sl  = slider(0, 20, 2)
        self._zrw_lbl = lbl("±2 bins", True)
        self._zrw_sl.valueChanged.connect(self._on_zrw_slide)
        lay.addWidget(self._zrw_sl)
        lay.addWidget(self._zrw_lbl)

        # Color levels
        lay.addWidget(lbl("Color floor (×0.1 log10)"))
        self._floor_sl  = slider(0, 80, 20)
        self._floor_lbl = lbl("2.0", True)
        self._floor_sl.valueChanged.connect(
            lambda v: self._floor_lbl.setText(f"{v/10:.1f}"))
        lay.addWidget(self._floor_sl)
        lay.addWidget(self._floor_lbl)

        lay.addWidget(lbl("Color ceiling (×0.1 log10)"))
        self._ceil_sl  = slider(20, 100, 80)
        self._ceil_lbl = lbl("8.0", True)
        self._ceil_sl.valueChanged.connect(
            lambda v: self._ceil_lbl.setText(f"{v/10:.1f}"))
        lay.addWidget(self._ceil_sl)
        lay.addWidget(self._ceil_lbl)

        auto_lv = QPushButton("Auto Level")
        auto_lv.setFixedHeight(24)
        auto_lv.clicked.connect(self._on_auto_level)
        lay.addWidget(auto_lv)

        # Recording
        self._save_cb  = QCheckBox("Record session")
        self._save_cb.setChecked(False)
        self._save_cb.stateChanged.connect(self._on_save_toggle)
        lay.addWidget(self._save_cb)
        self._save_lbl = lbl("Not recording", True)
        self._save_lbl.setStyleSheet("font-size: 10px; color: #888;")
        lay.addWidget(self._save_lbl)

        lay.addStretch()

        self._status_lbl = QLabel("Waiting for first frame…")
        self._status_lbl.setStyleSheet("font-size: 10px; color: #88aaff;")
        self._status_lbl.setWordWrap(True)
        lay.addWidget(self._status_lbl)

        quit_btn = QPushButton("Quit")
        quit_btn.setStyleSheet(
            "background-color: #662222; color: white; font-weight: bold;")
        quit_btn.setFixedHeight(30)
        quit_btn.clicked.connect(self._on_quit)
        lay.addWidget(quit_btn)

        return box

    def _build_plots(self) -> pg.GraphicsLayoutWidget:
        glw = pg.GraphicsLayoutWidget()
        r   = self._range_pos
        v   = self._vel_ms
        h   = self._args.history_len

        r_display = float(self._maxr_sl.value())
        # Use plain text labels — do NOT pass units= to pyqtgraph.
        # pyqtgraph applies SI prefix scaling to the unit string, which
        # converts "m" → "km" and "m/s" → "km/s" for values in typical
        # radar ranges, collapsing the near-range display to sub-pixel.
        label_kw  = {"color": "#ccc", "font-size": "10pt"}

        # ── Beat-frequency FFT panel (top-left) ───────────────────────
        p_rp = glw.addPlot(row=0, col=0,
                           title="Beat-Freq FFT  (mean chirp, dBFS)")
        p_rp.setLabel("bottom", "Range [m]",  **label_kw)
        p_rp.setLabel("left",   "dBFS",       **label_kw)
        p_rp.setXRange(0.0, r_display, padding=0)
        p_rp.setYRange(-100, 0, padding=0)
        p_rp.enableAutoRange("xy", False)
        p_rp.showGrid(x=True, y=True, alpha=0.2)

        self._fft_curve = p_rp.plot(
            pen=pg.mkPen(color=(255, 200, 50), width=1.5))
        self._rp_curve = p_rp.plot(
            pen=pg.mkPen(color=(255, 120, 30), width=1.0,
                         style=Qt.DotLine))
        self._cfar_curve = p_rp.plot(
            pen=pg.mkPen(color=(50, 200, 255), width=1.2,
                         style=Qt.DashLine))
        self._gate_line_rp = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=2),
            label="gate", labelOpts={"color": "#4f8", "position": 0.9},
        )
        p_rp.addItem(self._gate_line_rp)

        # ── Range-Doppler heatmap (top-right) ─────────────────────────
        p_rd = glw.addPlot(row=0, col=1, title="Range-Doppler")
        p_rd.setLabel("left",   "Range [m]",     **label_kw)
        p_rd.setLabel("bottom", "Velocity [m/s]", **label_kw)
        p_rd.setYRange(0.0, r_display, padding=0)
        p_rd.setXRange(float(v.min()), float(v.max()), padding=0)
        p_rd.enableAutoRange("xy", False)
        p_rd.showGrid(x=True, y=True, alpha=0.15)

        self._rd_img = pg.ImageItem()
        self._rd_img.setLookupTable(INFERNO_LUT)
        v_span = float(v.max() - v.min())
        r_span = float(r.max()) if len(r) else 10.0
        self._rd_img.setRect(pg.QtCore.QRectF(
            float(v.min()), 0.0, v_span, r_span))
        p_rd.addItem(self._rd_img)

        self._gate_line_rd = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=1.5,
                         style=Qt.DashLine),
        )
        p_rd.addItem(self._gate_line_rd)

        # ── Micro-Doppler history (bottom, full width) ─────────────────
        p_md = glw.addPlot(row=1, col=0, colspan=2,
                           title="Micro-Doppler History")
        p_md.setLabel("left",   "Velocity [m/s]", **label_kw)
        p_md.setLabel("bottom", "Frame index",     **label_kw)
        p_md.setXRange(0, h, padding=0)
        p_md.setYRange(float(v.min()), float(v.max()), padding=0)
        p_md.enableAutoRange("xy", False)
        p_md.showGrid(x=True, y=True, alpha=0.15)

        self._md_img = pg.ImageItem()
        self._md_img.setLookupTable(VIRIDIS_LUT)
        self._md_img.setRect(pg.QtCore.QRectF(
            0.0, float(v.min()), float(h), float(v.max() - v.min())))
        p_md.addItem(self._md_img)

        glw.ci.layout.setRowStretchFactor(0, 3)
        glw.ci.layout.setRowStretchFactor(1, 2)

        self._p_rp = p_rp
        self._p_rd = p_rd
        self._p_md = p_md

        return glw

    # ------------------------------------------------------------------
    # Data slot — called in GUI thread via Qt signal
    # ------------------------------------------------------------------

    def on_capture(self, cap: CaptureResult) -> None:
        t0  = time.monotonic()
        cfg = cap.cfg

        # 0. Raw beat-frequency FFT (pre-MTI, pre-2D) — baseline panel
        try:
            chirp_mean = cap.chirp_matrix.mean(axis=0)
            w_bl       = np.blackman(len(chirp_mean))
            fft_abs    = np.abs(np.fft.fftshift(np.fft.fft(chirp_mean * w_bl)))
            fft_dbfs   = 20.0 * np.log10(
                np.maximum(fft_abs / np.sum(w_bl), 1e-15) / 2.0**11
            )
            beat_fft_pos = fft_dbfs[self._pos_mask]
        except Exception:
            beat_fft_pos = None

        # 1. Full DSP pipeline — returns rd_map indexed on the FULL range axis
        result = process_frame(
            cap.chirp_matrix,
            apply_mti_filter=self._mti_cb.isChecked(),
            window=self._win_cb.isChecked(),
            range_axis_m=self._range_m,        # full axis (incl. negative half)
            min_range_m=self._args.min_range_m,
            min_scale=None,
            max_scale=None,
        )
        rd = result["rd_map"].copy()   # (n_frame, n_doppler) float32

        # 2. TX-leakage suppression on the full map
        if self._zrs_cb.isChecked():
            rd_min = rd.min()
            zrw    = self._zrw_sl.value()

            # Blank ±zrw bins around the zero-range bin
            zero_bin = int(np.argmin(np.abs(self._range_m)))
            rd[max(0, zero_bin - zrw) : min(rd.shape[0], zero_bin + zrw), :] = rd_min

            # Blank predicted IF-leakage Doppler columns
            n_dop            = rd.shape[1]
            half             = n_dop // 2
            leakage_offset   = int(round(
                cfg.signal_freq * cfg.ramp_time_s * cfg.num_chirps
            ))
            ldw = 3
            if leakage_offset > 0:
                for sign in (+1, -1):
                    col_c = half + sign * leakage_offset
                    rd[:, max(0, col_c - ldw) : min(n_dop, col_c + ldw + 1)] = rd_min

        # 3. Clip to positive-range half
        rd_pos      = rd[self._pos_mask, :]   # (n_pos, n_doppler)
        range_pos   = self._range_pos
        self._last_rd = rd_pos

        # 4. Range profile
        profile  = rd_pos.max(axis=1)

        # 5. Remap range_bin from full-axis index to positive-axis index
        sel_bin_full = result["range_bin"]
        if sel_bin_full < len(self._range_m):
            range_at_full = self._range_m[sel_bin_full]
            sel_bin_pos   = int(np.argmin(np.abs(range_pos - range_at_full)))
        else:
            sel_bin_pos = 0

        # 6. CFAR refinement
        cfar_thresh_arr = None
        if self._cfar_cb.isChecked():
            try:
                bias   = float(self._cfar_sl.value())
                thresh, _ = _cfar(
                    profile,
                    num_guard_cells=3,
                    num_ref_cells=8,
                    bias=bias,
                )
                cfar_thresh_arr = np.ma.filled(thresh, profile.min())
                min_r    = max(self._args.min_range_m, cfg.range_res_m)
                pos_mask2 = range_pos >= min_r
                above    = np.flatnonzero(
                    pos_mask2 & (profile > cfar_thresh_arr)
                )
                if above.size:
                    sel_bin_pos = int(above[np.argmax(profile[above])])
            except Exception:
                pass

        range_m  = float(range_pos[sel_bin_pos]) if sel_bin_pos < len(range_pos) else 0.0
        md_slice = rd_pos[sel_bin_pos, :].astype(np.float32)
        history  = self._history.push(md_slice)

        # 7. Color levels
        floor = self._floor_sl.value() / 10.0
        ceil  = self._ceil_sl.value()  / 10.0
        if ceil <= floor:
            ceil = floor + 0.1

        # 8. Update all panels
        if beat_fft_pos is not None:
            self._fft_curve.setData(x=range_pos, y=beat_fft_pos)
        if profile.max() > 0:
            p_norm = (profile / profile.max()) * 30.0 - 80.0
            self._rp_curve.setData(x=range_pos, y=p_norm)
        if cfar_thresh_arr is not None and cfar_thresh_arr.max() > 0:
            c_norm = (cfar_thresh_arr / (profile.max() + 1e-9)) * 30.0 - 80.0
            self._cfar_curve.setData(x=range_pos, y=c_norm)
        self._gate_line_rp.setValue(range_m)
        self._gate_line_rd.setValue(range_m)
        self._rd_img.setImage(rd_pos, levels=(floor, ceil))
        self._md_img.setImage(history.T, levels=(floor, ceil))

        # 9. Recording
        if self._saving:
            spc = cfg.n_frame
            nc  = cfg.num_chirps
            self._raw_bursts.append(cap.chirp_matrix.copy())
            self._spectrograms.append(md_slice.copy())
            self._valid_chirps.append(int(cap.chirp_matrix.shape[0]))
            self._timestamps.append(t0)
            if cap.ch0 is not None:
                self._raw_ch0.append(
                    np.asarray(cap.ch0, dtype=np.complex64).reshape(nc, spc))
            if cap.ch1 is not None:
                self._raw_ch1.append(
                    np.asarray(cap.ch1, dtype=np.complex64).reshape(nc, spc))

        # 10. Status
        dt = (time.monotonic() - t0) * 1000
        n  = len(self._raw_bursts) if self._saving else 0
        self._status_lbl.setText(
            f"gate: {range_m:.2f} m  bin: {sel_bin_pos}\n"
            f"proc: {dt:.0f} ms   saved: {n}"
        )

    # ------------------------------------------------------------------
    # Control callbacks
    # ------------------------------------------------------------------

    def _on_steer(self, value: int) -> None:
        self._steer_lbl.setText(f"{value}°")
        try:
            self._backend.set_steering_angle(float(value))
        except Exception as exc:
            print(f"[steer] {exc}")

    def _on_rx_gain_apply(self) -> None:
        gain = self._rxgain_sl.value()
        try:
            sdr = self._backend._sdr
            sdr.rx_hardwaregain_chan0 = gain
            sdr.rx_hardwaregain_chan1 = gain
            print(f"[RX gain] applied {gain} dB")
        except Exception as exc:
            print(f"[RX gain] failed: {exc}")

    def _on_max_range(self, value: int) -> None:
        self._maxr_lbl.setText(f"{value} m")
        self._p_rp.setXRange(0.0, float(value), padding=0)
        self._p_rd.setYRange(0.0, float(value), padding=0)

    def _on_zrw_slide(self, value: int) -> None:
        blank_m = value * self._cfg.range_res_m
        self._zrw_lbl.setText(f"±{value} bins  ({blank_m:.1f} m)")

    def _on_auto_level(self) -> None:
        if self._last_rd is None:
            return
        lo, hi = np.percentile(self._last_rd, [5.0, 99.5])
        lo = max(0.0, lo)
        hi = max(lo + 0.1, hi)
        self._floor_sl.setValue(int(lo * 10))
        self._ceil_sl.setValue(min(100, int(hi * 10)))

    def _on_save_toggle(self, state: int) -> None:
        self._saving = (state == Qt.Checked)
        if self._saving:
            self._raw_bursts.clear()
            self._raw_ch0.clear()
            self._raw_ch1.clear()
            self._spectrograms.clear()
            self._valid_chirps.clear()
            self._timestamps.clear()
            self._save_lbl.setText("● Recording…")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #ff8844;")
        else:
            self._flush_recording()

    def _flush_recording(self) -> None:
        if not self._raw_bursts:
            self._save_lbl.setText("Not recording")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #888;")
            return
        try:
            sdir = save_session(
                out_dir=self._args.save_dir,
                cfg=self._cfg,
                raw_bursts=np.stack(self._raw_bursts, axis=0),
                spectrograms=np.stack(self._spectrograms, axis=0),
                raw_ch0=(np.stack(self._raw_ch0, axis=0)
                         if self._raw_ch0 else None),
                raw_ch1=(np.stack(self._raw_ch1, axis=0)
                         if self._raw_ch1 else None),
                valid_chirps=np.array(self._valid_chirps, dtype=np.int32),
                metadata={
                    "acq":         self._args.acq,
                    "tdd_trigger": self._args.tdd_trigger,
                    "sdr_uri":     self._args.sdr_uri,
                    "mti":         self._mti_cb.isChecked(),
                    "window":      self._win_cb.isChecked(),
                    "cfar":        self._cfar_cb.isChecked(),
                    "cfar_bias":   self._cfar_sl.value(),
                },
            )
            np.save(sdir / "timestamps.npy",
                    np.array(self._timestamps, dtype=np.float64))
            n = len(self._raw_bursts)
            print(f"[v5] Saved {n} frames → {sdir}")
            self._save_lbl.setText(f"Saved {n} frames")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #88ff88;")
        except Exception as exc:
            print(f"[v5] Save failed: {exc}")
            self._save_lbl.setText("Save FAILED")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #ff4444;")

    def _on_quit(self) -> None:
        if self._saving:
            self._save_cb.setChecked(False)
        self.close()

    def closeEvent(self, event) -> None:
        if hasattr(self, "_worker"):
            self._worker.requestInterruption()
            self._worker.wait(3000)
        try:
            self._backend.close()
        except Exception:
            pass
        event.accept()


# ---------------------------------------------------------------------------
# Dark application palette
# ---------------------------------------------------------------------------

def _dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal    = QPalette()
    dark   = QColor(30,  30,  46)
    mid    = QColor(50,  50,  70)
    light  = QColor(220, 220, 220)
    accent = QColor(100, 150, 255)
    pal.setColor(QPalette.Window,          dark)
    pal.setColor(QPalette.WindowText,      light)
    pal.setColor(QPalette.Base,            QColor(20, 20, 35))
    pal.setColor(QPalette.AlternateBase,   mid)
    pal.setColor(QPalette.ToolTipBase,     mid)
    pal.setColor(QPalette.ToolTipText,     light)
    pal.setColor(QPalette.Text,            light)
    pal.setColor(QPalette.Button,          mid)
    pal.setColor(QPalette.ButtonText,      light)
    pal.setColor(QPalette.Highlight,       accent)
    pal.setColor(QPalette.HighlightedText, dark)
    app.setPalette(pal)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    taper = tuple(
        int(v.strip()) for v in args.gain_taper.split(",") if v.strip()
    )
    if len(taper) != 8:
        raise ValueError("--gain-taper must have exactly 8 values")

    hw = HardwareParams(
        rpi_uri       = args.rpi_uri,
        sdr_uri       = args.sdr_uri,
        tdd_uri       = args.tdd_uri,
        sample_rate   = args.sample_rate,
        center_freq   = args.center_freq,
        signal_freq   = args.signal_freq,
        rx_gain       = args.rx_gain,
        tx_gain       = args.tx_gain,
        output_freq   = args.output_freq,
        chirp_bw      = args.bw,
        ramp_time_us  = args.ramp_time_us,
        num_chirps    = args.num_chirps,
        gain_taper    = taper,
        tdd_trigger   = args.tdd_trigger,
        tdd_sync_external = (args.tdd_trigger == "external"),
        tdd_ext_capture   = (args.tdd_trigger == "external"),
        tdd_arm_delay_s   = args.tdd_arm_delay_s,
        tdd_rx_timeout_ms = args.tdd_rx_timeout_ms,
        frame_guard_ms    = args.frame_guard_ms,
    )

    print(
        f"[v5] Connecting…  acq={args.acq}  trigger={args.tdd_trigger}  "
        f"sdr={args.sdr_uri}"
    )
    backend = make_backend(args.acq, hw)
    cfg     = backend.connect()

    print(
        f"[v5] rf={cfg.output_freq/1e9:.4f} GHz  "
        f"bw={cfg.chirp_bw/1e6:.0f} MHz  "
        f"ramp={int(cfg.ramp_time_s*1e6)} us  "
        f"chirps={cfg.num_chirps}  "
        f"spc={cfg.n_frame}  "
        f"R_res={cfg.range_res_m:.3f} m  "
        f"v_res={cfg.vel_res_m_s:.3f} m/s  "
        f"max_vel=±{cfg.max_vel_m_s:.1f} m/s"
    )

    if args.steer != 0.0:
        backend.set_steering_angle(args.steer)

    app    = QApplication(sys.argv)
    _dark_palette(app)

    win    = SentinelWindow(backend, cfg, args)
    worker = RxWorker(backend)
    win._worker = worker

    worker.data_ready.connect(win.on_capture)
    worker.error.connect(
        lambda msg: print(f"[v5] RxWorker error: {msg}"))
    worker.start()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
