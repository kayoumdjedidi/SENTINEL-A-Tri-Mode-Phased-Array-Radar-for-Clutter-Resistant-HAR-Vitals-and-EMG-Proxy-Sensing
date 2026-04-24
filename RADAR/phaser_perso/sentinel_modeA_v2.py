#!/usr/bin/env python3
"""SENTINEL Mode A v2 — indoor-optimised FMCW Range-Doppler + Micro-Doppler.

Adds over sentinel_radar.py
----------------------------
  --acq {continuous,tdd}   acquisition mode selector
  Manual range gate slider  overrides CFAR when non-zero
  Labels CSV               recordings/<session>/labels.csv written per frame

Indoor defaults (1-5 m in a 5x5 m space)
------------------------------------------
  --bw 500e6 --ramp 1200 --chirps 128 --sample-rate 0.6e6
  → R_res=0.30 m, 2m target at 6.7 bins from leakage (baseline match)
  → 128 chirps × 1200 µs = 153.6 ms CPI, v_res=0.49 m/s, max_range=10 m

Usage
-----
  python sentinel_modeA_v2.py
  python sentinel_modeA_v2.py --acq tdd --sdr-uri ip:192.168.2.1
  python sentinel_modeA_v2.py --bw 200e6 --ramp 200   # SENTINEL spec params
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import replace
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
from radar_backends import (
    CaptureResult,
    HardwareParams,
    make_backend,
    _BaseBackend,
)
from target_detection_dbfs import cfar as _cfar

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
        prog="sentinel_modeA_v2",
        description="SENTINEL Mode A v2 — indoor FMCW RD + Micro-Doppler",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rpi-uri", default="ip:phaser.local")
    p.add_argument("--sdr-uri", default="ip:phaser.local:50901")
    p.add_argument("--acq", choices=["continuous", "tdd"], default="continuous",
                   help="Acquisition mode. Use 'tdd' with direct Pluto URI only.")
    p.add_argument("--bw", type=float, default=500e6, metavar="Hz",
                   help="Chirp bandwidth. 500 MHz → R_res=0.30 m (indoor-optimal)")
    p.add_argument("--ramp", type=int, default=500, metavar="us",
                   help="Ramp time µs. sep_bins=2*BW*R/c is independent of ramp — "
                        "500 µs gives 64 ms CPI at 128 chirps (~15 fps).")
    p.add_argument("--chirps", type=int, default=128,
                   help="Chirps per frame")
    p.add_argument("--output-freq", type=float, default=10.25e9, metavar="Hz")
    p.add_argument("--rx-gain", type=int, default=70)
    p.add_argument("--sample-rate", type=float, default=0.6e6, metavar="Hz",
                   help="ADC sample rate. 0.6 MHz fits over 100 Mbit Ethernet.")
    p.add_argument("--history-len", type=int, default=200)
    p.add_argument("--save-dir", type=Path, default=Path("recordings"))
    return p


# ---------------------------------------------------------------------------
# RX worker
# ---------------------------------------------------------------------------

class RxWorker(QThread):
    data_ready = pyqtSignal(object)
    error      = pyqtSignal(str)

    def __init__(self, backend: _BaseBackend) -> None:
        super().__init__()
        self._backend = backend

    def run(self) -> None:
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

    def __init__(self, backend: _BaseBackend, cfg, args: argparse.Namespace) -> None:
        super().__init__()
        self._backend  = backend
        self._cfg      = cfg
        self._args     = args
        self._acq_mode = args.acq

        self._range_m   = cfg.range_axis()
        self._vel_ms    = cfg.velocity_axis()
        self._pos_mask  = self._range_m >= 0.0
        self._range_pos = self._range_m[self._pos_mask]
        self._last_rd:   np.ndarray | None = None
        self._clutter_map: np.ndarray | None = None   # EMA background for RD subtraction

        # Cache axis extents — used to re-enforce plot ranges every frame
        self._v_min = float(self._vel_ms.min())
        self._v_max = float(self._vel_ms.max())
        self._v_span = self._v_max - self._v_min

        self._history = MicroDopplerHistory(
            history_len=args.history_len,
            doppler_bins=cfg.num_chirps,
        )

        # Fixed background state — user stands clear, presses Capture BG,
        # radar averages N frames, stores the empty-room RD map.
        # Any new frame has this subtracted → person appears as residual.
        self._fixed_bg:      np.ndarray | None = None
        self._bg_capturing:  bool              = False
        self._bg_accum:      np.ndarray | None = None
        self._bg_count:      int               = 0
        self._bg_target:     int               = 45   # ~3s at 15fps

        # Recording state
        self._saving        = False
        self._frame_idx     = 0
        self._raw_bursts:   list[np.ndarray] = []
        self._raw_ch0:      list[np.ndarray] = []
        self._raw_ch1:      list[np.ndarray] = []
        self._spectrograms: list[np.ndarray] = []
        self._timestamps:   list[float]      = []
        self._labels_path:  Path | None      = None
        self._labels_file                    = None
        self._labels_writer                  = None

        self.setWindowTitle(self._make_title())
        self._build_ui()
        self.showMaximized()

    def _make_title(self) -> str:
        cfg = self._cfg
        return (
            f"SENTINEL Mode A v2  |  {self._acq_mode.upper()}  |  "
            f"{cfg.chirp_bw/1e6:.0f} MHz  "
            f"{int(cfg.ramp_time_s*1e6)} µs  {cfg.num_chirps} chirps  "
            f"R_res={cfg.range_res_m:.2f} m  v_res={cfg.vel_res_m_s:.2f} m/s"
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root        = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(6)
        self.setCentralWidget(root)
        root_layout.addWidget(self._build_controls(), stretch=0)
        root_layout.addWidget(self._build_plots(),    stretch=1)

    def _build_controls(self) -> QWidget:
        box = QGroupBox("SENTINEL v2 Controls")
        box.setFixedWidth(260)
        lay = QVBoxLayout(box)
        lay.setSpacing(4)

        def lbl(text, small=False):
            w = QLabel(text)
            w.setStyleSheet(f"font-size: {'10' if small else '11'}px; color: #ccc;")
            return w

        def hslider(lo, hi, val, tick=0):
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(val)
            if tick:
                s.setTickInterval(tick)
                s.setTickPosition(QSlider.TicksBelow)
            return s

        # ── Acq mode display ──────────────────────────────────────────
        acq_lbl = QLabel(f"Acq mode: {self._acq_mode.upper()}")
        acq_lbl.setStyleSheet("font-size: 10px; color: #88aaff; font-weight: bold;")
        lay.addWidget(acq_lbl)

        # ── Beam steering ─────────────────────────────────────────────
        lay.addWidget(lbl("Beam Steering (°)"))
        self._steer_sl  = hslider(-80, 80, 0, tick=20)
        self._steer_lbl = lbl("0°", small=True)
        self._steer_sl.valueChanged.connect(self._on_steer)
        lay.addWidget(self._steer_sl)
        lay.addWidget(self._steer_lbl)

        # ── RX Gain ───────────────────────────────────────────────────
        lay.addWidget(lbl("RX Gain (dB)"))
        self._rxgain_sl  = hslider(-3, 70, self._args.rx_gain, tick=10)
        self._rxgain_lbl = lbl(f"{self._args.rx_gain} dB", small=True)
        self._rxgain_sl.valueChanged.connect(
            lambda v: self._rxgain_lbl.setText(f"{v} dB")
        )
        apply_gain = QPushButton("Apply RX Gain")
        apply_gain.setFixedHeight(24)
        apply_gain.clicked.connect(self._on_rx_gain_apply)
        lay.addWidget(self._rxgain_sl)
        lay.addWidget(self._rxgain_lbl)
        lay.addWidget(apply_gain)

        # ── Bandwidth ─────────────────────────────────────────────────
        lay.addWidget(lbl("Bandwidth (MHz)"))
        self._bw_sl  = hslider(100, 500, int(self._cfg.chirp_bw / 1e6), tick=100)
        self._bw_lbl = lbl(
            f"{self._cfg.chirp_bw/1e6:.0f} MHz  R_res={self._cfg.range_res_m:.2f} m",
            small=True,
        )
        self._bw_sl.valueChanged.connect(self._on_bw_slide)
        apply_bw = QPushButton("Apply BW")
        apply_bw.setFixedHeight(24)
        apply_bw.clicked.connect(self._on_bw_apply)
        lay.addWidget(self._bw_sl)
        lay.addWidget(self._bw_lbl)
        lay.addWidget(apply_bw)

        # ── Max display range ─────────────────────────────────────────
        lay.addWidget(lbl("Max Display Range (m)"))
        r_max_phys = float(self._range_pos.max()) if len(self._range_pos) else 10.0
        r_max_init = max(5, min(10, int(r_max_phys)))   # default 10 m (indoor)
        self._maxr_sl  = hslider(2, 50, r_max_init, tick=5)
        self._maxr_lbl = lbl(f"{r_max_init} m", small=True)
        self._maxr_sl.valueChanged.connect(self._on_max_range)
        lay.addWidget(self._maxr_sl)
        lay.addWidget(self._maxr_lbl)

        # ── Manual range gate ─────────────────────────────────────────
        # 0 = Auto (CFAR controls gate), 1-100 = 0.1-10.0 m locked gate
        lay.addWidget(lbl("Manual Gate (0=Auto, step=0.1 m)"))
        self._gate_sl  = hslider(0, 100, 0, tick=10)
        self._gate_lbl = lbl("Auto (CFAR)", small=True)
        self._gate_sl.valueChanged.connect(self._on_gate_slide)
        release_gate = QPushButton("Release Gate (Auto)")
        release_gate.setFixedHeight(24)
        release_gate.clicked.connect(lambda: self._gate_sl.setValue(0))
        lay.addWidget(self._gate_sl)
        lay.addWidget(self._gate_lbl)
        lay.addWidget(release_gate)

        # ── DSP toggles ───────────────────────────────────────────────
        self._mti_cb  = QCheckBox("MTI clutter filter")
        self._mti_cb.setChecked(False)   # OFF: show raw 2D FFT like ADI reference
        self._win_cb  = QCheckBox("Hanning window")
        self._win_cb.setChecked(True)
        self._cfar_cb = QCheckBox("CFAR range gate")
        self._cfar_cb.setChecked(True)
        self._zrs_cb  = QCheckBox("Suppress TX leakage (range+Doppler)")
        self._zrs_cb.setChecked(True)
        for cb in (self._mti_cb, self._win_cb, self._cfar_cb, self._zrs_cb):
            lay.addWidget(cb)

        # ── CFAR bias ─────────────────────────────────────────────────
        lay.addWidget(lbl("CFAR bias (dB)"))
        self._cfar_sl  = hslider(0, 50, 15)
        self._cfar_lbl = lbl("15 dB", small=True)
        self._cfar_sl.valueChanged.connect(
            lambda v: self._cfar_lbl.setText(f"{v} dB")
        )
        lay.addWidget(self._cfar_sl)
        lay.addWidget(self._cfar_lbl)

        # ── Zero-range blanking ───────────────────────────────────────
        lay.addWidget(lbl("Zero-range blank (bins ±)"))
        self._zrw_sl  = hslider(0, 20, 2)
        self._zrw_lbl = lbl("±2 bins", small=True)
        self._zrw_sl.valueChanged.connect(self._on_zrw_slide)
        lay.addWidget(self._zrw_sl)
        lay.addWidget(self._zrw_lbl)

        # ── Leakage Doppler blank ─────────────────────────────────────
        lay.addWidget(lbl("Leakage Doppler blank (cols ±)"))
        self._ldw_sl  = hslider(0, 12, 5)
        self._ldw_lbl = lbl("±5 cols", small=True)
        self._ldw_sl.valueChanged.connect(
            lambda v: self._ldw_lbl.setText(f"±{v} cols")
        )
        lay.addWidget(self._ldw_sl)
        lay.addWidget(self._ldw_lbl)

        # ── DC Doppler blank (removes v=0 static-clutter stripe) ──────
        # Blanks columns around v=0 Doppler (DC) in the 2D RD map.
        # This removes the bright vertical stripe from walls/floor/static objects.
        # Does NOT blank moving targets (v≠0). Key for indoor HAR.
        lay.addWidget(lbl("DC Doppler blank (cols ±)"))
        self._dcw_sl  = hslider(0, 15, 3)
        self._dcw_lbl = lbl("±3 cols", small=True)
        self._dcw_sl.valueChanged.connect(
            lambda v: self._dcw_lbl.setText(f"±{v} cols")
        )
        lay.addWidget(self._dcw_sl)
        lay.addWidget(self._dcw_lbl)

        # ── EMA clutter subtraction ───────────────────────────────────
        self._ema_cb = QCheckBox("EMA clutter subtract (2D)")
        self._ema_cb.setChecked(False)   # OFF by default — causes periodic artifacts
        self._ema_cb.stateChanged.connect(
            lambda _: self._reset_clutter_map()
        )
        lay.addWidget(self._ema_cb)

        lay.addWidget(lbl("EMA alpha (0=fast, 99=slow)"))
        self._ema_sl  = hslider(50, 99, 95)   # /100 → 0.95 default  (~20-frame TC)
        self._ema_lbl = lbl("α=0.95", small=True)
        self._ema_sl.valueChanged.connect(
            lambda v: self._ema_lbl.setText(f"α={v/100:.2f}")
        )
        lay.addWidget(self._ema_sl)
        lay.addWidget(self._ema_lbl)

        reset_clutter = QPushButton("Reset Clutter Map")
        reset_clutter.setFixedHeight(24)
        reset_clutter.clicked.connect(self._reset_clutter_map)
        lay.addWidget(reset_clutter)

        # Debug: bypass EMA+MTI to see raw RD — useful for confirming hardware
        # detects the user before enabling motion-only processing.
        self._raw_rd_cb = QCheckBox("Raw RD (disable MTI+EMA — debug)")
        self._raw_rd_cb.setChecked(False)
        self._raw_rd_cb.setStyleSheet("font-size: 10px; color: #ffcc44;")
        lay.addWidget(self._raw_rd_cb)

        # ── Fixed background subtraction ──────────────────────────────
        # Procedure: stand CLEAR of the scene, click Capture BG, wait 3s.
        # Then step in front — you'll appear as a bright residual even static.
        lay.addWidget(lbl("Fixed Background Subtraction"))
        self._bg_cb = QCheckBox("Subtract fixed BG")
        self._bg_cb.setChecked(False)
        lay.addWidget(self._bg_cb)

        self._bg_btn = QPushButton("Capture BG (stand clear, 3s)")
        self._bg_btn.setFixedHeight(26)
        self._bg_btn.clicked.connect(self._on_capture_bg)
        lay.addWidget(self._bg_btn)

        clear_bg = QPushButton("Clear Fixed BG")
        clear_bg.setFixedHeight(24)
        clear_bg.clicked.connect(self._on_clear_bg)
        lay.addWidget(clear_bg)

        self._bg_lbl = lbl("No BG captured", small=True)
        self._bg_lbl.setStyleSheet("font-size: 10px; color: #888;")
        lay.addWidget(self._bg_lbl)

        # ── Color levels ──────────────────────────────────────────────
        lay.addWidget(lbl("Color floor (×0.1 log10)"))
        self._floor_sl  = hslider(0, 80, 50)   # 5.0 default — fits raw log10 RD
        self._floor_lbl = lbl("5.0", small=True)
        self._floor_sl.valueChanged.connect(
            lambda v: self._floor_lbl.setText(f"{v/10:.1f}")
        )
        lay.addWidget(self._floor_sl)
        lay.addWidget(self._floor_lbl)

        lay.addWidget(lbl("Color ceiling (×0.1 log10)"))
        self._ceil_sl  = hslider(20, 100, 80)
        self._ceil_lbl = lbl("8.0", small=True)
        self._ceil_sl.valueChanged.connect(
            lambda v: self._ceil_lbl.setText(f"{v/10:.1f}")
        )
        lay.addWidget(self._ceil_sl)
        lay.addWidget(self._ceil_lbl)

        auto_lv = QPushButton("Auto Level (once)")
        auto_lv.setFixedHeight(24)
        auto_lv.clicked.connect(self._on_auto_level)
        lay.addWidget(auto_lv)

        self._autoscale_cb = QCheckBox("Auto-scale every frame")
        self._autoscale_cb.setChecked(False)  # OFF — causes whole-view flicker
        self._autoscale_cb.setStyleSheet("font-size: 10px; color: #aaffaa;")
        lay.addWidget(self._autoscale_cb)

        # ── Recording ─────────────────────────────────────────────────
        self._save_cb = QCheckBox("Record session")
        self._save_cb.setChecked(False)
        self._save_cb.stateChanged.connect(self._on_save_toggle)
        lay.addWidget(self._save_cb)
        self._save_lbl = lbl("Not recording", small=True)
        self._save_lbl.setStyleSheet("font-size: 10px; color: #888;")
        lay.addWidget(self._save_lbl)

        lay.addStretch()

        self._status_lbl = QLabel("Waiting for first frame…")
        self._status_lbl.setStyleSheet("font-size: 10px; color: #88aaff;")
        self._status_lbl.setWordWrap(True)
        lay.addWidget(self._status_lbl)

        quit_btn = QPushButton("Quit")
        quit_btn.setStyleSheet("background-color: #662222; color: white; font-weight: bold;")
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
        label_kw  = {"color": "#ccc", "font-size": "10pt"}

        # ── Beat-freq FFT ─────────────────────────────────────────────
        p_rp = glw.addPlot(row=0, col=0, title="Beat-Freq FFT  (mean chirp, dBFS)")
        p_rp.setLabel("bottom", "Range",  units="m",   **label_kw)
        p_rp.setLabel("left",   "dBFS",               **label_kw)
        p_rp.setXRange(0.0, r_display, padding=0)
        p_rp.setYRange(-100, 0, padding=0)
        p_rp.enableAutoRange("xy", False)
        p_rp.showGrid(x=True, y=True, alpha=0.2)

        self._fft_curve  = p_rp.plot(pen=pg.mkPen(color=(255, 200, 50),  width=1.5))
        self._rp_curve   = p_rp.plot(pen=pg.mkPen(color=(255, 120, 30),  width=1.0, style=Qt.DotLine))
        self._cfar_curve = p_rp.plot(pen=pg.mkPen(color=(50,  200, 255), width=1.2, style=Qt.DashLine))
        self._gate_line_rp = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=2),
            label="gate", labelOpts={"color": "#4f8", "position": 0.9},
        )
        p_rp.addItem(self._gate_line_rp)

        # ── Range-Doppler ─────────────────────────────────────────────
        v_min  = float(v.min())
        v_max  = float(v.max())
        v_span = v_max - v_min
        r_span = float(r.max()) if len(r) else 10.0

        p_rd = glw.addPlot(row=0, col=1,
                           title=f"Range-Doppler  (v: {v_min:.1f}…{v_max:.1f} m/s)")
        p_rd.setLabel("left",   "Range (m)",    **label_kw)
        p_rd.setLabel("bottom", "Velocity (m/s)", **label_kw)
        p_rd.setYRange(0.0, r_display, padding=0)
        p_rd.setXRange(v_min, v_max, padding=0)
        p_rd.enableAutoRange("xy", False)
        p_rd.showGrid(x=True, y=True, alpha=0.15)

        self._rd_img = pg.ImageItem()
        self._rd_img.setLookupTable(INFERNO_LUT)
        self._rd_img.setRect(pg.QtCore.QRectF(v_min, 0.0, v_span, r_span))
        p_rd.addItem(self._rd_img)

        self._gate_line_rd = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=1.5, style=Qt.DashLine),
        )
        p_rd.addItem(self._gate_line_rd)

        # ── Micro-Doppler history ─────────────────────────────────────
        p_md = glw.addPlot(row=1, col=0, colspan=2, title="Micro-Doppler History")
        p_md.setLabel("left",   "Velocity (m/s)", **label_kw)
        p_md.setLabel("bottom", "Frame index",    **label_kw)
        p_md.setXRange(0, h, padding=0)
        p_md.setYRange(v_min, v_max, padding=0)
        p_md.enableAutoRange("xy", False)
        p_md.showGrid(x=True, y=True, alpha=0.15)

        self._md_img = pg.ImageItem()
        self._md_img.setLookupTable(VIRIDIS_LUT)
        self._md_img.setRect(pg.QtCore.QRectF(0.0, v_min, float(h), v_span))
        p_md.addItem(self._md_img)

        glw.ci.layout.setRowStretchFactor(0, 3)
        glw.ci.layout.setRowStretchFactor(1, 2)

        self._p_rp = p_rp
        self._p_rd = p_rd
        self._p_md = p_md
        return glw

    # ------------------------------------------------------------------
    # Capture slot
    # ------------------------------------------------------------------

    def on_capture(self, cap: CaptureResult) -> None:
        t0  = time.monotonic()
        cfg = cap.cfg

        # ── Beat-freq FFT (pre-MTI, baseline-style) ───────────────────
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

        raw_rd_mode = self._raw_rd_cb.isChecked()

        # ── DSP pipeline ─────────────────────────────────────────────
        result = process_frame(
            cap.chirp_matrix,
            apply_mti_filter=(self._mti_cb.isChecked() and not raw_rd_mode),
            window=self._win_cb.isChecked(),
            range_axis_m=self._range_m,
            min_range_m=0.3,
            min_scale=None,
            max_scale=None,
        )
        rd = result["rd_map"].copy()

        # ── TX-leakage suppression ────────────────────────────────────
        if self._zrs_cb.isChecked():
            rd_min = rd.min()
            zrw    = self._zrw_sl.value()
            zero_bin = int(np.argmin(np.abs(self._range_m)))
            lo_r = max(0, zero_bin - zrw)
            hi_r = min(rd.shape[0], zero_bin + zrw)
            rd[lo_r:hi_r, :] = rd_min

            n_dop = rd.shape[1]
            half  = n_dop // 2

            leakage_offset = int(round(
                cfg.signal_freq * cfg.ramp_time_s * cfg.num_chirps
            ))
            ldw = self._ldw_sl.value()
            if leakage_offset > 0 and ldw > 0:
                for sign in (+1, -1):
                    col_c = half + sign * leakage_offset
                    lo_c  = max(0,     col_c - ldw)
                    hi_c  = min(n_dop, col_c + ldw + 1)
                    rd[:, lo_c:hi_c] = rd_min

            # DC Doppler blank: remove the v=0 static-clutter stripe.
            # This blanks indoor static clutter (walls/floor) from the RD display
            # while preserving moving targets at v≠0.  Applied before positive clip
            # so the DC column is suppressed across all ranges.
            dcw = self._dcw_sl.value()
            if dcw > 0:
                lo_dc = max(0,     half - dcw)
                hi_dc = min(n_dop, half + dcw + 1)
                rd[:, lo_dc:hi_dc] = rd_min

        # ── Positive range clip ───────────────────────────────────────
        rd_pos    = rd[self._pos_mask, :]
        range_pos = self._range_pos

        # ── Fixed background accumulation ─────────────────────────────
        if self._bg_capturing:
            if self._bg_accum is None or self._bg_accum.shape != rd_pos.shape:
                self._bg_accum = rd_pos.astype(np.float64)
                self._bg_count = 1
            else:
                self._bg_accum += rd_pos
                self._bg_count += 1
            remaining = self._bg_target - self._bg_count
            self._bg_lbl.setText(f"Capturing… {self._bg_count}/{self._bg_target} ({remaining} left)")
            if self._bg_count >= self._bg_target:
                self._fixed_bg  = (self._bg_accum / self._bg_count).astype(np.float32)
                self._bg_capturing = False
                self._bg_accum  = None
                self._bg_btn.setEnabled(True)
                self._bg_cb.setChecked(True)
                self._bg_lbl.setText(f"BG captured ({self._bg_count} frames) ✓")
                self._bg_lbl.setStyleSheet("font-size: 10px; color: #88ff88;")

        # ── Clutter subtraction (optional, one stage only) ────────────
        # Priority: Fixed BG > EMA > none (raw).
        # The two modes are mutually exclusive in the subtraction stage to
        # avoid the double-subtraction instability seen previously.
        rd_display = rd_pos.copy()
        if not raw_rd_mode:
            if self._bg_cb.isChecked() and self._fixed_bg is not None \
                    and self._fixed_bg.shape == rd_pos.shape:
                # Fixed background captured without person present.
                # Person (even static) shows as a residual above the empty scene.
                rd_display = np.maximum(rd_pos - self._fixed_bg, 0.0)
            elif self._ema_cb.isChecked():
                # EMA-only (no fixed BG): adapts to current scene.
                # Warning: person absorbed into background after ~TC frames.
                alpha = self._ema_sl.value() / 100.0
                if self._clutter_map is None or self._clutter_map.shape != rd_pos.shape:
                    self._clutter_map = rd_pos.copy()
                else:
                    self._clutter_map = alpha * self._clutter_map + (1.0 - alpha) * rd_pos
                rd_display = np.maximum(rd_pos - self._clutter_map, 0.0)

        self._last_rd = rd_display

        profile     = rd_display.max(axis=1)
        sel_bin     = result["range_bin"]
        sel_bin_pos = int(np.argmin(np.abs(range_pos - self._range_m[sel_bin]))) \
            if sel_bin < len(self._range_m) else 0

        # ── Manual gate override or CFAR ──────────────────────────────
        manual_gate_m = self._gate_sl.value() * 0.1   # 0.0 = auto
        cfar_thresh_arr = None

        if manual_gate_m > 0.05:
            # Lock gate at slider position
            sel_bin_pos = int(np.argmin(np.abs(range_pos - manual_gate_m)))
        elif self._cfar_cb.isChecked():
            try:
                bias = float(self._cfar_sl.value())
                thresh, _ = _cfar(profile, num_guard_cells=3, num_ref_cells=8, bias=bias)
                cfar_thresh_arr = np.ma.filled(thresh, profile.min())
                blank_m = self._zrw_sl.value() * self._cfg.range_res_m
                min_r   = max(0.3, blank_m + self._cfg.range_res_m)
                pos_mask2 = range_pos >= min_r
                above = np.flatnonzero(pos_mask2 & (profile > cfar_thresh_arr))
                if above.size:
                    sel_bin_pos = int(above[np.argmax(profile[above])])
            except Exception:
                pass

        range_m = float(range_pos[sel_bin_pos]) if sel_bin_pos < len(range_pos) else 0.0

        # ── Micro-Doppler ─────────────────────────────────────────────
        # Use rd_pos (raw 2D FFT with DC blank, pre-EMA/BG subtraction) so
        # the history always contains real Doppler content — not EMA residuals.
        md_slice = rd_pos[sel_bin_pos, :].astype(np.float32)
        history  = self._history.push(md_slice)

        # ── Display levels ────────────────────────────────────────────
        if self._autoscale_cb.isChecked() and rd_display.size > 0:
            lo, hi = np.percentile(rd_display, [2.0, 99.5])
            lo  = max(0.0, float(lo))
            hi  = max(lo + 1e-9, float(hi))
            floor, ceil = lo, hi
        else:
            floor = self._floor_sl.value() / 10.0
            ceil  = self._ceil_sl.value()  / 10.0
            if ceil <= floor:
                ceil = floor + 0.1

        # ── Plot updates ──────────────────────────────────────────────
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

        # Clip RD to display range so the image rect always matches the data shape
        r_disp  = float(self._maxr_sl.value())
        disp_ok = range_pos <= r_disp
        rd_clipped = rd_display[disp_ok, :]
        r_clipped  = range_pos[disp_ok]
        r_span_clipped = float(r_clipped[-1]) if r_clipped.size else r_disp

        # Re-enforce the physical coordinate rect every frame (pyqtgraph can
        # override it with auto-range if image shape changes between frames)
        v_min, v_span = self._v_min, self._v_span
        self._rd_img.setRect(pg.QtCore.QRectF(v_min, 0.0, v_span, r_span_clipped))
        self._rd_img.setImage(rd_clipped, levels=(floor, ceil))
        self._p_rd.setXRange(v_min, self._v_max, padding=0)
        self._p_rd.setYRange(0.0, r_disp, padding=0)

        h = self._args.history_len
        self._md_img.setRect(pg.QtCore.QRectF(0.0, v_min, float(h), v_span))
        self._md_img.setImage(history.T, levels=(floor, ceil))
        self._p_md.setYRange(v_min, self._v_max, padding=0)

        # ── Recording + labels ────────────────────────────────────────
        if self._saving:
            self._raw_bursts.append(cap.chirp_matrix.copy())
            self._spectrograms.append(md_slice.copy())
            self._timestamps.append(t0)
            if cap.ch0 is not None:
                self._raw_ch0.append(cap.ch0.copy())
            if cap.ch1 is not None:
                self._raw_ch1.append(cap.ch1.copy())

            if self._labels_writer is not None:
                self._labels_writer.writerow({
                    "frame_idx":    self._frame_idx,
                    "timestamp":    f"{t0:.6f}",
                    "gate_range_m": f"{range_m:.3f}",
                    "gate_source":  "manual" if manual_gate_m > 0.05 else "cfar",
                    "acq_mode":     self._acq_mode,
                    "bw_hz":        int(self._cfg.chirp_bw),
                    "ramp_us":      int(self._cfg.ramp_time_s * 1e6),
                })

        self._frame_idx += 1

        # ── Status bar ────────────────────────────────────────────────
        dt      = (time.monotonic() - t0) * 1000
        n       = len(self._raw_bursts) if self._saving else 0
        blank_m = self._zrw_sl.value() * cfg.range_res_m
        gate_src = "manual" if manual_gate_m > 0.05 else "CFAR"
        self._status_lbl.setText(
            f"gate: {range_m:.2f} m  bin: {sel_bin_pos}  [{gate_src}]\n"
            f"blanked: 0–{blank_m:.1f} m\n"
            f"proc: {dt:.0f} ms   saved: {n}   frame: {self._frame_idx}"
        )

    # ------------------------------------------------------------------
    # Control callbacks
    # ------------------------------------------------------------------

    def _reset_clutter_map(self) -> None:
        self._clutter_map = None

    def _on_capture_bg(self) -> None:
        self._bg_capturing = True
        self._bg_accum  = None
        self._bg_count  = 0
        self._fixed_bg  = None
        self._bg_btn.setEnabled(False)
        self._bg_cb.setChecked(False)
        self._bg_lbl.setText("Capturing… 0/45")
        self._bg_lbl.setStyleSheet("font-size: 10px; color: #ffcc44;")

    def _on_clear_bg(self) -> None:
        self._fixed_bg  = None
        self._bg_capturing = False
        self._bg_accum  = None
        self._bg_count  = 0
        self._bg_cb.setChecked(False)
        self._bg_btn.setEnabled(True)
        self._bg_lbl.setText("No BG captured")
        self._bg_lbl.setStyleSheet("font-size: 10px; color: #888;")

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
            print(f"[RX gain] {exc}")

    def _on_bw_slide(self, value: int) -> None:
        r_res = 3e8 / (2.0 * value * 1e6)
        self._bw_lbl.setText(f"{value} MHz  R_res={r_res:.2f} m")

    def _on_bw_apply(self) -> None:
        bw        = self._bw_sl.value() * 1e6
        num_steps = int(self._cfg.ramp_time_s * 1e6)
        try:
            ph = self._backend._phaser
            ph.freq_dev_range = int(bw / 4)
            ph.freq_dev_step  = int((bw / 4) / max(num_steps, 1))
            ph.enable = 0
            print(f"[BW] applied {bw/1e6:.0f} MHz")
        except Exception as exc:
            print(f"[BW] {exc}")
            return
        self._cfg       = replace(self._cfg, chirp_bw=bw)
        self._range_m   = self._cfg.range_axis()
        self._pos_mask  = self._range_m >= 0.0
        self._range_pos = self._range_m[self._pos_mask]
        self._reset_clutter_map()
        r, v = self._range_pos, self._vel_ms
        r_display = float(self._maxr_sl.value())
        self._p_rp.setXRange(0.0, r_display, padding=0)
        self._p_rd.setYRange(0.0, r_display, padding=0)
        self._rd_img.setRect(pg.QtCore.QRectF(
            float(v.min()), 0.0,
            float(v.max() - v.min()),
            float(r.max()) if len(r) else 10.0,
        ))
        self.setWindowTitle(self._make_title())

    def _on_max_range(self, value: int) -> None:
        self._maxr_lbl.setText(f"{value} m")
        self._p_rp.setXRange(0.0, float(value), padding=0)
        self._p_rd.setYRange(0.0, float(value), padding=0)
        # Extend gate slider range to match
        self._gate_sl.setMaximum(value * 10)

    def _on_gate_slide(self, value: int) -> None:
        if value == 0:
            self._gate_lbl.setText("Auto (CFAR)")
        else:
            self._gate_lbl.setText(f"Locked: {value * 0.1:.1f} m")

    def _on_zrw_slide(self, value: int) -> None:
        r_res = self._cfg.range_res_m
        self._zrw_lbl.setText(f"±{value} bins  ({value * r_res:.1f} m)")

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
            self._timestamps.clear()
            self._save_lbl.setText("● Recording…")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #ff8844;")
        else:
            self._flush_recording()

    def _open_labels_file(self, sdir: Path) -> None:
        self._labels_path   = sdir / "labels.csv"
        f = open(self._labels_path, "w", newline="")
        self._labels_file   = f
        fieldnames = [
            "frame_idx", "timestamp", "gate_range_m",
            "gate_source", "acq_mode", "bw_hz", "ramp_us",
        ]
        self._labels_writer = csv.DictWriter(f, fieldnames=fieldnames)
        self._labels_writer.writeheader()

    def _close_labels_file(self) -> None:
        if self._labels_file is not None:
            try:
                self._labels_file.flush()
                self._labels_file.close()
            except Exception:
                pass
            self._labels_file   = None
            self._labels_writer = None

    def _flush_recording(self) -> None:
        self._close_labels_file()
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
                metadata={
                    "mti":        self._mti_cb.isChecked(),
                    "window":     self._win_cb.isChecked(),
                    "cfar":       self._cfar_cb.isChecked(),
                    "cfar_bias":  self._cfar_sl.value(),
                    "zrs":        self._zrs_cb.isChecked(),
                    "acq_mode":   self._acq_mode,
                },
            )
            # Move labels.csv into the session directory if it was opened early
            # (when save_session creates the dir, the labels file was already open)
            if self._labels_path and self._labels_path.exists():
                target = sdir / "labels.csv"
                if self._labels_path != target:
                    self._labels_path.replace(target)
                    self._labels_path = target

            if self._raw_ch0:
                np.save(sdir / "raw_ch0.npy",
                        np.stack(self._raw_ch0, axis=0).astype(np.complex64))
            if self._raw_ch1:
                np.save(sdir / "raw_ch1.npy",
                        np.stack(self._raw_ch1, axis=0).astype(np.complex64))
            np.save(sdir / "timestamps.npy",
                    np.array(self._timestamps, dtype=np.float64))

            n = len(self._raw_bursts)
            print(f"[sentinel_v2] Saved {n} frames → {sdir}")
            self._save_lbl.setText(f"Saved {n} frames")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #88ff88;")
        except Exception as exc:
            print(f"[sentinel_v2] Save failed: {exc}")
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
        self._close_labels_file()
        try:
            self._backend.close()
        except Exception:
            pass
        event.accept()


# ---------------------------------------------------------------------------
# Labels file is opened once the recording directory is known.
# We patch _on_save_toggle to create the dir early so labels can be written
# incrementally during the session.
# ---------------------------------------------------------------------------

def _patch_save_toggle(win: SentinelWindow) -> None:
    original = win._on_save_toggle

    def patched(state: int) -> None:
        original(state)
        if win._saving:
            # Create a provisional labels dir under save_dir
            prov = win._args.save_dir / "_recording_in_progress"
            prov.mkdir(parents=True, exist_ok=True)
            win._open_labels_file(prov)

    win._on_save_toggle = patched
    win._save_cb.stateChanged.disconnect()
    win._save_cb.stateChanged.connect(patched)


# ---------------------------------------------------------------------------
# Dark palette
# ---------------------------------------------------------------------------

def _dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal    = QPalette()
    dark   = QColor(30, 30, 46)
    mid    = QColor(50, 50, 70)
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
    parser = build_parser()
    args   = parser.parse_args(argv)

    print(f"[sentinel_v2] Connecting — acq={args.acq} …")
    hw = HardwareParams(
        rpi_uri      = args.rpi_uri,
        sdr_uri      = args.sdr_uri,
        sample_rate  = args.sample_rate,
        center_freq  = 2.1e9,
        signal_freq  = 100e3,
        rx_gain      = args.rx_gain,
        tx_gain      = 0,
        output_freq  = args.output_freq,
        chirp_bw     = args.bw,
        ramp_time_us = args.ramp,
        num_chirps   = args.chirps,
        gain_taper   = CHEBYSHEV_TAPER,
    )
    backend = make_backend(args.acq, hw)
    cfg     = backend.connect()

    # Diagnostic: target-leakage separation
    slope      = cfg.chirp_bw / cfg.ramp_time_s
    bin_hz     = cfg.sample_rate / cfg.n_frame
    beat_2m_hz = 2.0 * slope * 2.0 / 3e8
    sep_bins   = beat_2m_hz / bin_hz
    leakage_doppler_offset = int(round(cfg.signal_freq * cfg.ramp_time_s * cfg.num_chirps))
    print(
        f"[sentinel_v2] "
        f"rf={cfg.output_freq/1e9:.4f} GHz  "
        f"bw={cfg.chirp_bw/1e6:.0f} MHz  "
        f"ramp={int(cfg.ramp_time_s*1e6)} µs  "
        f"fs={cfg.sample_rate/1e6:.3f} MHz  "
        f"chirps={cfg.num_chirps}  spc={cfg.n_frame}  "
        f"R_res={cfg.range_res_m:.3f} m  v_res={cfg.vel_res_m_s:.3f} m/s\n"
        f"[sentinel_v2] velocity axis: ±{cfg.max_vel_m_s:.2f} m/s  "
        f"(prf={cfg.prf:.1f} Hz  λ={cfg.wavelength*100:.2f} cm)\n"
        f"[sentinel_v2] 2m target: beat={beat_2m_hz/1e3:.1f} kHz  "
        f"bin={bin_hz:.0f} Hz  sep={sep_bins:.1f} bins from leakage  "
        f"({'GOOD' if sep_bins >= 5 else 'WARN: target may be masked'})\n"
        f"[sentinel_v2] leakage Doppler offset = ±{leakage_doppler_offset} bins from DC  "
        f"(blank ≥{leakage_doppler_offset+3} bins total with default ldw=5)"
    )

    app = QApplication(sys.argv)
    _dark_palette(app)

    win         = SentinelWindow(backend, cfg, args)
    _patch_save_toggle(win)

    worker      = RxWorker(backend)
    win._worker = worker
    worker.data_ready.connect(win.on_capture)
    worker.error.connect(lambda msg: print(f"[RX error] {msg}"))
    worker.start()

    win.show()
    ret = app.exec()

    worker.requestInterruption()
    worker.wait(3000)
    try:
        backend.close()
    except Exception:
        pass
    return ret


if __name__ == "__main__":
    sys.exit(main())
