#!/usr/bin/env python3
"""SENTINEL Mode A v4 — continuous FMCW with static-phase deskew.

Why this exists
---------------
`sentinel_modeA_v3.py` still assumed that the persistent off-DC stripe could be
predicted purely from the nominal leakage math. In practice the forwarded Pluto
path can still leave a frame-to-frame common phase ramp and the fixed background
path in v3 subtracts log-scaled maps directly, which collapses to black.

v4 changes only the pieces that match the observed failure mode:
- chirp-wise common-phase deskew before RD formation
- empirical leakage-column detection from near-range energy
- fixed-background subtraction in linear magnitude, not in log space
- range gating from the beat-frequency profile instead of the already-corrupted
  range-Doppler image

This keeps the hardware/backends unchanged and attacks the DSP/display path.

Usage
-----
  python sentinel_modeA_v4.py
  python sentinel_modeA_v4.py --acq tdd --sdr-uri ip:192.168.2.1
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
    QApplication, QCheckBox, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QSlider, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from radar_dsp import MicroDopplerHistory, apply_mti, save_session
from radar_backends import CaptureResult, HardwareParams, make_backend, _BaseBackend
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


def _deskew_chirps(chirp_matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Remove chirp-to-chirp common phase drift using chirp 0 as reference."""
    chirps = np.asarray(chirp_matrix, dtype=np.complex64)
    if chirps.ndim != 2 or chirps.shape[0] == 0:
        return chirps.copy(), np.zeros(0, dtype=np.float32)

    ref = chirps[0]
    out = chirps.copy()
    phases = np.zeros(chirps.shape[0], dtype=np.float32)
    for idx in range(chirps.shape[0]):
        corr = np.vdot(ref, chirps[idx])
        phase = float(np.angle(corr)) if np.abs(corr) > 0 else 0.0
        phases[idx] = phase
        out[idx] *= np.complex64(np.exp(-1j * phase))
    return out, phases


def _range_doppler_linear(chirp_matrix: np.ndarray, *, window: bool) -> np.ndarray:
    """2D FFT returning linear magnitude with shape (range_bins, doppler_bins)."""
    chirps = np.asarray(chirp_matrix, dtype=np.complex64)
    if window:
        w_d = np.hanning(chirps.shape[0]).astype(np.float32)
        w_r = np.hanning(chirps.shape[1]).astype(np.float32)
        chirps = chirps * w_d[:, np.newaxis] * w_r[np.newaxis, :]
    return np.fft.fftshift(np.abs(np.fft.fft2(chirps))).T.astype(np.float32, copy=False)


def _to_log10(data: np.ndarray, floor: float = 1e-12) -> np.ndarray:
    return np.log10(np.maximum(np.asarray(data, dtype=np.float32), floor))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel_modeA_v4",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rpi-uri", default="ip:phaser.local")
    p.add_argument("--sdr-uri", default="ip:phaser.local:50901")
    p.add_argument("--acq", choices=["continuous", "tdd"], default="continuous")
    p.add_argument("--bw", type=float, default=500e6,
                   help="500 MHz → R_res=0.30 m")
    p.add_argument("--ramp", type=int, default=505,
                   help="505 µs → n_frame=303 exact, leakage at Nyquist edge (Hanning-suppressed)")
    p.add_argument("--chirps", type=int, default=128)
    p.add_argument("--output-freq", type=float, default=10.25e9)
    p.add_argument("--rx-gain", type=int, default=70)
    p.add_argument("--sample-rate", type=float, default=0.6e6)
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
                self.data_ready.emit(self._backend.capture())
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

        self._v_min  = float(self._vel_ms.min())
        self._v_max  = float(self._vel_ms.max())
        self._v_span = self._v_max - self._v_min

        self._history = MicroDopplerHistory(
            history_len=args.history_len, doppler_bins=cfg.num_chirps
        )

        # Fixed background (optional, captured without person)
        self._fixed_bg:     np.ndarray | None = None
        self._bg_capturing: bool              = False
        self._bg_accum:     np.ndarray | None = None
        self._bg_count:     int               = 0
        self._bg_target:    int               = 45

        # Recording
        self._saving        = False
        self._frame_idx     = 0
        self._raw_bursts:   list[np.ndarray] = []
        self._spectrograms: list[np.ndarray] = []
        self._timestamps:   list[float]      = []
        self._raw_ch0:      list[np.ndarray] = []
        self._raw_ch1:      list[np.ndarray] = []
        self._labels_path:  Path | None      = None
        self._labels_file                    = None
        self._labels_writer                  = None

        self.setWindowTitle(self._title())
        self._build_ui()
        self.showMaximized()

    def _title(self) -> str:
        c = self._cfg
        n_dop = c.num_chirps
        chirp_dt = c.n_frame / c.sample_rate
        raw   = int(round(c.signal_freq * chirp_dt * n_dop))
        leak_wrapped = raw % n_dop
        return (
            f"SENTINEL v4  |  {self._acq_mode.upper()}  |  "
            f"{c.chirp_bw/1e6:.0f} MHz  {int(c.ramp_time_s*1e6)} µs  "
            f"{n_dop} chirps  R={c.range_res_m:.2f}m  "
            f"v={c.vel_res_m_s:.2f}m/s  "
            f"leak=±{leak_wrapped}bins"
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        lay  = QHBoxLayout(root)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
        self.setCentralWidget(root)
        lay.addWidget(self._build_controls(), stretch=0)
        lay.addWidget(self._build_plots(),    stretch=1)

    def _build_controls(self) -> QWidget:
        box = QGroupBox("SENTINEL v4")
        box.setFixedWidth(240)
        lay = QVBoxLayout(box)
        lay.setSpacing(3)

        def lbl(text, small=False):
            w = QLabel(text)
            w.setStyleSheet(f"font-size: {'10' if small else '11'}px; color: #ccc;")
            return w

        def slider(lo, hi, val, tick=0):
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(val)
            if tick:
                s.setTickInterval(tick); s.setTickPosition(QSlider.TicksBelow)
            return s

        # Mode info
        info = QLabel(f"{self._acq_mode.upper()}  ramp={self._args.ramp}µs")
        info.setStyleSheet("font-size:10px; color:#88aaff; font-weight:bold;")
        lay.addWidget(info)

        # Steering
        lay.addWidget(lbl("Beam Steering (°)"))
        self._steer_sl  = slider(-80, 80, 0, tick=20)
        self._steer_lbl = lbl("0°", small=True)
        self._steer_sl.valueChanged.connect(self._on_steer)
        lay.addWidget(self._steer_sl); lay.addWidget(self._steer_lbl)

        # RX gain
        lay.addWidget(lbl("RX Gain (dB)"))
        self._rxgain_sl  = slider(-3, 70, self._args.rx_gain, tick=10)
        self._rxgain_lbl = lbl(f"{self._args.rx_gain} dB", small=True)
        self._rxgain_sl.valueChanged.connect(
            lambda v: self._rxgain_lbl.setText(f"{v} dB"))
        btn = QPushButton("Apply RX Gain"); btn.setFixedHeight(22)
        btn.clicked.connect(self._on_rx_gain_apply)
        lay.addWidget(self._rxgain_sl); lay.addWidget(self._rxgain_lbl)
        lay.addWidget(btn)

        # Max display range
        lay.addWidget(lbl("Max Range (m)"))
        r_init = max(5, min(10, int(float(self._range_pos.max()) if len(self._range_pos) else 10)))
        self._maxr_sl  = slider(2, 50, r_init, tick=5)
        self._maxr_lbl = lbl(f"{r_init} m", small=True)
        self._maxr_sl.valueChanged.connect(self._on_max_range)
        lay.addWidget(self._maxr_sl); lay.addWidget(self._maxr_lbl)

        # Manual gate
        lay.addWidget(lbl("Manual Gate (0=auto, ×0.1 m)"))
        self._gate_sl  = slider(0, 100, 0, tick=10)
        self._gate_lbl = lbl("Auto (CFAR)", small=True)
        self._gate_sl.valueChanged.connect(self._on_gate_slide)
        btn2 = QPushButton("Release Gate"); btn2.setFixedHeight(22)
        btn2.clicked.connect(lambda: self._gate_sl.setValue(0))
        lay.addWidget(self._gate_sl); lay.addWidget(self._gate_lbl)
        lay.addWidget(btn2)

        # DSP toggles
        self._deskew_cb = QCheckBox("Deskew static phase to DC")
        self._deskew_cb.setChecked(True)
        self._mti_cb = QCheckBox("MTI (inter-chirp clutter cancel)")
        self._mti_cb.setChecked(False)
        self._win_cb = QCheckBox("Hanning window")
        self._win_cb.setChecked(True)
        self._zrs_cb = QCheckBox("Blank measured leakage column")
        self._zrs_cb.setChecked(True)
        lay.addWidget(self._deskew_cb)
        lay.addWidget(self._mti_cb)
        lay.addWidget(self._win_cb)
        lay.addWidget(self._zrs_cb)

        # Leakage Doppler blank width
        lay.addWidget(lbl("Leakage blank width (cols ±)"))
        self._ldw_sl  = slider(0, 12, 5)
        self._ldw_lbl = lbl("±5 cols", small=True)
        self._ldw_sl.valueChanged.connect(
            lambda v: self._ldw_lbl.setText(f"±{v} cols"))
        lay.addWidget(self._ldw_sl); lay.addWidget(self._ldw_lbl)

        # DC Doppler blank
        lay.addWidget(lbl("DC blank (static-clutter cols ±)"))
        self._dcw_sl  = slider(0, 10, 3)
        self._dcw_lbl = lbl("±3 cols", small=True)
        self._dcw_sl.valueChanged.connect(
            lambda v: self._dcw_lbl.setText(f"±{v} cols"))
        lay.addWidget(self._dcw_sl); lay.addWidget(self._dcw_lbl)

        # Zero-range blank
        lay.addWidget(lbl("Zero-range blank (bins ±)"))
        self._zrw_sl  = slider(0, 20, 2)
        self._zrw_lbl = lbl("±2 bins", small=True)
        self._zrw_sl.valueChanged.connect(self._on_zrw_slide)
        lay.addWidget(self._zrw_sl); lay.addWidget(self._zrw_lbl)

        # CFAR
        self._cfar_cb = QCheckBox("CFAR gate")
        self._cfar_cb.setChecked(True)
        lay.addWidget(self._cfar_cb)
        lay.addWidget(lbl("CFAR bias (dB)"))
        self._cfar_sl  = slider(0, 50, 15)
        self._cfar_lbl = lbl("15 dB", small=True)
        self._cfar_sl.valueChanged.connect(
            lambda v: self._cfar_lbl.setText(f"{v} dB"))
        lay.addWidget(self._cfar_sl); lay.addWidget(self._cfar_lbl)

        # Color levels
        lay.addWidget(lbl("Color floor (×0.1 log10)"))
        self._floor_sl  = slider(0, 80, 50)
        self._floor_lbl = lbl("5.0", small=True)
        self._floor_sl.valueChanged.connect(
            lambda v: self._floor_lbl.setText(f"{v/10:.1f}"))
        lay.addWidget(self._floor_sl); lay.addWidget(self._floor_lbl)

        lay.addWidget(lbl("Color ceil (×0.1 log10)"))
        self._ceil_sl  = slider(20, 100, 80)
        self._ceil_lbl = lbl("8.0", small=True)
        self._ceil_sl.valueChanged.connect(
            lambda v: self._ceil_lbl.setText(f"{v/10:.1f}"))
        lay.addWidget(self._ceil_sl); lay.addWidget(self._ceil_lbl)

        btn_al = QPushButton("Auto Level"); btn_al.setFixedHeight(22)
        btn_al.clicked.connect(self._on_auto_level)
        lay.addWidget(btn_al)

        # Fixed background subtraction
        lay.addWidget(lbl("── Fixed BG subtraction ──", small=True))
        self._bg_cb = QCheckBox("Subtract fixed BG (linear)")
        self._bg_cb.setChecked(False)
        lay.addWidget(self._bg_cb)
        btn_bg = QPushButton("Capture BG (stand clear, 3s)")
        btn_bg.setFixedHeight(24)
        btn_bg.clicked.connect(self._on_capture_bg)
        self._bg_btn = btn_bg
        lay.addWidget(btn_bg)
        btn_clrbg = QPushButton("Clear Fixed BG"); btn_clrbg.setFixedHeight(22)
        btn_clrbg.clicked.connect(self._on_clear_bg)
        lay.addWidget(btn_clrbg)
        self._bg_lbl = lbl("No BG", small=True)
        self._bg_lbl.setStyleSheet("font-size:10px; color:#888;")
        lay.addWidget(self._bg_lbl)

        # Recording
        self._save_cb = QCheckBox("Record session")
        self._save_cb.setChecked(False)
        self._save_cb.stateChanged.connect(self._on_save_toggle)
        lay.addWidget(self._save_cb)
        self._save_lbl = lbl("Not recording", small=True)
        self._save_lbl.setStyleSheet("font-size:10px; color:#888;")
        lay.addWidget(self._save_lbl)

        lay.addStretch()

        self._status_lbl = QLabel("Waiting…")
        self._status_lbl.setStyleSheet("font-size:10px; color:#88aaff;")
        self._status_lbl.setWordWrap(True)
        lay.addWidget(self._status_lbl)

        btn_q = QPushButton("Quit")
        btn_q.setStyleSheet("background:#662222; color:white; font-weight:bold;")
        btn_q.setFixedHeight(28)
        btn_q.clicked.connect(self._on_quit)
        lay.addWidget(btn_q)

        return box

    def _build_plots(self) -> pg.GraphicsLayoutWidget:
        glw = pg.GraphicsLayoutWidget()
        v_min, v_max, v_span = self._v_min, self._v_max, self._v_span
        r_span  = float(self._range_pos.max()) if len(self._range_pos) else 10.0
        r_disp  = float(self._maxr_sl.value())
        h       = self._args.history_len
        lkw     = {"color": "#ccc", "font-size": "10pt"}

        # Beat-Freq FFT
        p_rp = glw.addPlot(row=0, col=0, title="Beat-Freq FFT  (mean chirp, dBFS)")
        p_rp.setLabel("bottom", "Range (m)",  **lkw)
        p_rp.setLabel("left",   "dBFS",       **lkw)
        p_rp.setXRange(0.0, r_disp, padding=0)
        p_rp.setYRange(-100, 0, padding=0)
        p_rp.enableAutoRange("xy", False)
        p_rp.showGrid(x=True, y=True, alpha=0.2)
        self._fft_curve  = p_rp.plot(pen=pg.mkPen((255, 200, 50),  width=1.5))
        self._cfar_curve = p_rp.plot(pen=pg.mkPen((50, 200, 255),  width=1.0, style=Qt.DashLine))
        self._gate_rp    = pg.InfiniteLine(angle=90, movable=False,
                               pen=pg.mkPen((80, 255, 120), width=2),
                               label="gate", labelOpts={"color":"#4f8","position":0.9})
        p_rp.addItem(self._gate_rp)

        # Range-Doppler
        p_rd = glw.addPlot(row=0, col=1,
                           title=f"Range-Doppler  (v: {v_min:.1f}…{v_max:.1f} m/s)")
        p_rd.setLabel("left",   "Range (m)",       **lkw)
        p_rd.setLabel("bottom", "Velocity (m/s)",  **lkw)
        p_rd.setYRange(0.0, r_disp, padding=0)
        p_rd.setXRange(v_min, v_max, padding=0)
        p_rd.enableAutoRange("xy", False)
        p_rd.showGrid(x=True, y=True, alpha=0.15)
        self._rd_img = pg.ImageItem()
        self._rd_img.setLookupTable(INFERNO_LUT)
        self._rd_img.setRect(pg.QtCore.QRectF(v_min, 0.0, v_span, r_span))
        p_rd.addItem(self._rd_img)
        self._gate_rd = pg.InfiniteLine(angle=0, movable=False,
                            pen=pg.mkPen((80, 255, 120), width=1.5, style=Qt.DashLine))
        p_rd.addItem(self._gate_rd)

        # Micro-Doppler history
        p_md = glw.addPlot(row=1, col=0, colspan=2, title="Micro-Doppler History")
        p_md.setLabel("left",   "Velocity (m/s)", **lkw)
        p_md.setLabel("bottom", "Frame index",    **lkw)
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

        # ── Beat-Freq FFT (pre-MTI, shows presence even when still) ──
        try:
            mean_chirp = cap.chirp_matrix.mean(axis=0)
            w_bl   = np.blackman(len(mean_chirp))
            beat_mag = (
                np.abs(np.fft.fftshift(np.fft.fft(mean_chirp * w_bl))) / np.sum(w_bl)
            ).astype(np.float32)
            fft_db = 20.0 * np.log10(np.maximum(beat_mag, 1e-15) / 2.0**11)
            beat_fft_pos = fft_db[self._pos_mask]
            beat_mag_pos = beat_mag[self._pos_mask]
        except Exception:
            beat_fft_pos = None
            beat_mag_pos = None

        # ── Chirp conditioning + 2D FFT in linear magnitude ───────────
        chirps = np.asarray(cap.chirp_matrix, dtype=np.complex64)
        deskew_rms_deg = 0.0
        if self._deskew_cb.isChecked():
            chirps, deskew_phase = _deskew_chirps(chirps)
            if deskew_phase.size:
                deskew_rms_deg = float(np.degrees(np.sqrt(np.mean(deskew_phase**2))))
        if self._mti_cb.isChecked():
            chirps = apply_mti(chirps)

        rd_lin = _range_doppler_linear(chirps, window=self._win_cb.isChecked())

        # ── Blanking ───────────────────────────────────────────────────
        n_r, n_dop = rd_lin.shape
        half    = n_dop // 2
        rd_work = rd_lin.copy()

        # 1. Near-zero range (TX coupling)
        zrw = self._zrw_sl.value()
        if zrw > 0:
            z = int(np.argmin(np.abs(self._range_m)))
            rd_work[max(0, z-zrw):min(n_r, z+zrw+1), :] = 0.0

        # 2. Leakage Doppler (measured from near-range energy, not assumed)
        leak_col = None
        if self._zrs_cb.isChecked():
            ldw = self._ldw_sl.value()
            if ldw > 0:
                range_pos = self._range_pos
                rd_probe = rd_work[self._pos_mask, :]
                blank_m = max(cfg.range_res_m, zrw * cfg.range_res_m)
                probe_mask = (range_pos >= blank_m) & (range_pos <= min(1.5, float(range_pos.max())))
                if not np.any(probe_mask):
                    probe_mask = range_pos >= blank_m
                if np.any(probe_mask):
                    dop_profile = rd_probe[probe_mask].mean(axis=0)
                    candidate = dop_profile.copy()
                    candidate[max(0, half-2):min(n_dop, half+3)] = 0.0
                    if np.any(candidate > 0):
                        leak_col = int(np.argmax(candidate))
                        peak = float(candidate[leak_col])
                        noise = float(np.median(candidate[candidate > 0])) if np.any(candidate > 0) else 0.0
                        if peak > max(4.0 * noise, 1e-6):
                            mirror = (2 * half - leak_col) % n_dop
                            for col in {leak_col, mirror}:
                                lo = max(0, col - ldw)
                                hi = min(n_dop, col + ldw + 1)
                                rd_work[:, lo:hi] = 0.0

        # 3. DC Doppler (static indoor clutter at v=0)
        dcw = self._dcw_sl.value()
        if dcw > 0:
            rd_work[:, max(0, half-dcw):min(n_dop, half+dcw+1)] = 0.0

        # ── Positive range only ────────────────────────────────────────
        rd_pos    = rd_work[self._pos_mask, :]
        range_pos = self._range_pos

        # ── Fixed BG subtraction in linear magnitude ──────────────────
        if self._bg_capturing:
            if self._bg_accum is None or self._bg_accum.shape != rd_pos.shape:
                self._bg_accum = rd_pos.astype(np.float64); self._bg_count = 1
            else:
                self._bg_accum += rd_pos; self._bg_count += 1
            self._bg_lbl.setText(f"Capturing {self._bg_count}/{self._bg_target}…")
            if self._bg_count >= self._bg_target:
                self._fixed_bg  = (self._bg_accum / self._bg_target).astype(np.float32)
                self._bg_capturing = False; self._bg_accum = None
                self._bg_btn.setEnabled(True)
                self._bg_cb.setChecked(True)
                self._bg_lbl.setText("BG captured ✓")
                self._bg_lbl.setStyleSheet("font-size:10px; color:#88ff88;")

        rd_display_lin = rd_pos.copy()
        if self._bg_cb.isChecked() and self._fixed_bg is not None \
                and self._fixed_bg.shape == rd_pos.shape:
            rd_display_lin = np.maximum(rd_pos - self._fixed_bg, 0.0)
        rd_display = _to_log10(rd_display_lin)

        # ── Range gate from beat FFT profile, not RD artifact maxima ──
        manual_m = self._gate_sl.value() * 0.1
        profile_lin = beat_mag_pos if beat_mag_pos is not None else rd_display_lin.max(axis=1)
        profile_db = (
            beat_fft_pos
            if beat_fft_pos is not None
            else 20.0 * np.log10(np.maximum(profile_lin, 1e-15))
        )
        cfar_thresh = None

        if manual_m > 0.05:
            gate_bin = int(np.argmin(np.abs(range_pos - manual_m)))
            gate_src = "manual"
        else:
            blank_m = max(0.3, (zrw + 1) * cfg.range_res_m)
            valid_rows = np.flatnonzero(range_pos >= blank_m)
            if valid_rows.size:
                gate_bin = int(valid_rows[np.argmax(profile_lin[valid_rows])])
            else:
                gate_bin = int(np.argmax(profile_lin))
            gate_src = "beat-max"
            if self._cfar_cb.isChecked():
                try:
                    thresh, _ = _cfar(profile_db, num_guard_cells=3, num_ref_cells=8,
                                      bias=float(self._cfar_sl.value()))
                    cfar_thresh = np.ma.filled(thresh, float(np.min(profile_db)))
                    above  = np.flatnonzero(
                        (range_pos >= blank_m) & (profile_db > cfar_thresh)
                    )
                    if above.size:
                        gate_bin = int(above[np.argmax(profile_lin[above])])
                        gate_src = "beat-cfar"
                except Exception:
                    pass

        gate_m = float(range_pos[gate_bin]) if gate_bin < len(range_pos) else 0.0

        # ── Micro-Doppler history from the displayed linear residual ───
        md_slice_lin = rd_display_lin[gate_bin, :].astype(np.float32)
        md_slice = _to_log10(md_slice_lin).astype(np.float32)
        history  = self._history.push(md_slice)

        # ── Gate lines ────────────────────────────────────────────────
        self._gate_rp.setValue(gate_m)
        self._gate_rd.setValue(gate_m)

        # ── Color levels ──────────────────────────────────────────────
        floor = self._floor_sl.value() / 10.0
        ceil  = self._ceil_sl.value()  / 10.0
        if ceil <= floor:
            ceil = floor + 0.1

        # ── Plot updates (re-enforce axis ranges every frame) ──────────
        r_disp = float(self._maxr_sl.value())
        v_min, v_max, v_span = self._v_min, self._v_max, self._v_span
        h = self._args.history_len

        if beat_fft_pos is not None:
            self._fft_curve.setData(x=range_pos, y=beat_fft_pos)
        if cfar_thresh is not None:
            self._cfar_curve.setData(x=range_pos, y=cfar_thresh)
        else:
            self._cfar_curve.setData([], [])

        disp_ok    = range_pos <= r_disp
        rd_clip    = rd_display[disp_ok, :]
        r_clip_max = float(range_pos[disp_ok][-1]) if disp_ok.any() else r_disp
        self._rd_img.setRect(pg.QtCore.QRectF(v_min, 0.0, v_span, r_clip_max))
        self._rd_img.setImage(rd_clip, levels=(floor, ceil))
        self._p_rd.setXRange(v_min, v_max, padding=0)
        self._p_rd.setYRange(0.0, r_disp, padding=0)

        self._md_img.setRect(pg.QtCore.QRectF(0.0, v_min, float(h), v_span))
        self._md_img.setImage(history.T, levels=(floor, ceil))
        self._p_md.setYRange(v_min, v_max, padding=0)

        # ── Recording ─────────────────────────────────────────────────
        if self._saving:
            self._raw_bursts.append(cap.chirp_matrix.copy())
            self._spectrograms.append(md_slice.copy())
            self._timestamps.append(t0)
            if cap.ch0 is not None: self._raw_ch0.append(cap.ch0.copy())
            if cap.ch1 is not None: self._raw_ch1.append(cap.ch1.copy())
            if self._labels_writer:
                self._labels_writer.writerow({
                    "frame": self._frame_idx, "t": f"{t0:.6f}",
                    "gate_m": f"{gate_m:.3f}", "gate_src": gate_src,
                    "acq": self._acq_mode,
                })

        self._frame_idx += 1
        dt = (time.monotonic() - t0) * 1000
        leak_text = "n/a" if leak_col is None else f"col {leak_col}"
        self._status_lbl.setText(
            f"gate: {gate_m:.2f} m  [{gate_src}]\n"
            f"proc: {dt:.0f} ms  frame: {self._frame_idx}  leak: {leak_text}\n"
            f"deskew_rms: {deskew_rms_deg:.1f}°"
        )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_steer(self, v: int) -> None:
        self._steer_lbl.setText(f"{v}°")
        try:
            self._backend.set_steering_angle(float(v))
        except Exception as e:
            print(f"[steer] {e}")

    def _on_rx_gain_apply(self) -> None:
        g = self._rxgain_sl.value()
        try:
            self._backend._sdr.rx_hardwaregain_chan0 = g
            self._backend._sdr.rx_hardwaregain_chan1 = g
        except Exception as e:
            print(f"[rx_gain] {e}")

    def _on_max_range(self, v: int) -> None:
        self._maxr_lbl.setText(f"{v} m")
        self._p_rp.setXRange(0.0, float(v), padding=0)
        self._p_rd.setYRange(0.0, float(v), padding=0)
        self._gate_sl.setMaximum(v * 10)

    def _on_gate_slide(self, v: int) -> None:
        self._gate_lbl.setText("Auto (CFAR)" if v == 0 else f"Locked {v*0.1:.1f} m")

    def _on_zrw_slide(self, v: int) -> None:
        self._zrw_lbl.setText(f"±{v} bins ({v*self._cfg.range_res_m:.1f} m)")

    def _on_auto_level(self) -> None:
        rd = self._rd_img.image
        if rd is None or rd.size == 0:
            return
        vals = np.asarray(rd, dtype=np.float32).ravel()
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            return
        nz = vals[vals > -8.0]
        if nz.size >= 32:
            vals = nz
        lo, hi = float(np.percentile(vals, 5)), float(np.percentile(vals, 99.5))
        lo = max(0.0, lo)
        hi = max(lo + 0.1, hi)
        self._floor_sl.setValue(int(lo * 10))
        self._ceil_sl.setValue(min(100, int(hi * 10)))

    def _on_capture_bg(self) -> None:
        self._bg_capturing = True; self._bg_accum = None; self._bg_count = 0
        self._fixed_bg = None; self._bg_cb.setChecked(False)
        self._bg_btn.setEnabled(False)
        self._bg_lbl.setText(f"Capturing 0/{self._bg_target}…")
        self._bg_lbl.setStyleSheet("font-size:10px; color:#ffcc44;")

    def _on_clear_bg(self) -> None:
        self._fixed_bg = None; self._bg_capturing = False
        self._bg_accum = None; self._bg_count = 0
        self._bg_cb.setChecked(False); self._bg_btn.setEnabled(True)
        self._bg_lbl.setText("No BG")
        self._bg_lbl.setStyleSheet("font-size:10px; color:#888;")

    def _on_save_toggle(self, state: int) -> None:
        self._saving = (state == Qt.Checked)
        if self._saving:
            for lst in (self._raw_bursts, self._spectrograms,
                        self._timestamps, self._raw_ch0, self._raw_ch1):
                lst.clear()
            self._save_lbl.setText("● Recording…")
            self._save_lbl.setStyleSheet("font-size:10px; color:#ff8844;")
        else:
            self._flush_recording()

    def _flush_recording(self) -> None:
        if self._labels_file:
            try: self._labels_file.close()
            except Exception: pass
            self._labels_file = self._labels_writer = None
        if not self._raw_bursts:
            self._save_lbl.setText("Not recording")
            self._save_lbl.setStyleSheet("font-size:10px; color:#888;")
            return
        try:
            sdir = save_session(
                out_dir=self._args.save_dir, cfg=self._cfg,
                raw_bursts=np.stack(self._raw_bursts),
                spectrograms=np.stack(self._spectrograms),
                metadata={"acq": self._acq_mode, "mti": self._mti_cb.isChecked()},
            )
            if self._raw_ch0: np.save(sdir/"raw_ch0.npy",
                np.stack(self._raw_ch0).astype(np.complex64))
            if self._raw_ch1: np.save(sdir/"raw_ch1.npy",
                np.stack(self._raw_ch1).astype(np.complex64))
            np.save(sdir/"timestamps.npy",
                    np.array(self._timestamps, dtype=np.float64))
            # Write labels CSV
            with open(sdir/"labels.csv", "w", newline="") as f:
                w = csv.DictWriter(f, ["frame","t","gate_m","gate_src","acq"])
                w.writeheader()
            n = len(self._raw_bursts)
            print(f"[v4] Saved {n} frames → {sdir}")
            self._save_lbl.setText(f"Saved {n} frames")
            self._save_lbl.setStyleSheet("font-size:10px; color:#88ff88;")
        except Exception as exc:
            print(f"[v4] Save error: {exc}")
            self._save_lbl.setText("Save FAILED")
            self._save_lbl.setStyleSheet("font-size:10px; color:#ff4444;")

    def _on_quit(self) -> None:
        if self._saving: self._save_cb.setChecked(False)
        self.close()

    def closeEvent(self, event) -> None:
        if hasattr(self, "_worker"):
            self._worker.requestInterruption(); self._worker.wait(3000)
        try: self._backend.close()
        except Exception: pass
        event.accept()


# ---------------------------------------------------------------------------
# Dark palette
# ---------------------------------------------------------------------------

def _dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window,        QColor(30, 30, 46))
    pal.setColor(QPalette.WindowText,    QColor(220, 220, 220))
    pal.setColor(QPalette.Base,          QColor(20, 20, 35))
    pal.setColor(QPalette.AlternateBase, QColor(50, 50, 70))
    pal.setColor(QPalette.Text,          QColor(220, 220, 220))
    pal.setColor(QPalette.Button,        QColor(50, 50, 70))
    pal.setColor(QPalette.ButtonText,    QColor(220, 220, 220))
    pal.setColor(QPalette.Highlight,     QColor(100, 150, 255))
    app.setPalette(pal)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args   = build_parser().parse_args(argv)
    print(f"[v4] Connecting — acq={args.acq} …")

    hw = HardwareParams(
        rpi_uri=args.rpi_uri, sdr_uri=args.sdr_uri,
        sample_rate=args.sample_rate, center_freq=2.1e9,
        signal_freq=100e3, rx_gain=args.rx_gain, tx_gain=0,
        output_freq=args.output_freq,
        chirp_bw=args.bw, ramp_time_us=args.ramp, num_chirps=args.chirps,
        gain_taper=CHEBYSHEV_TAPER,
    )
    backend = make_backend(args.acq, hw)
    cfg     = backend.connect()

    # Diagnostics — use n_frame/sample_rate (actual segmentation period, not ramp_time_s)
    n_dop     = cfg.num_chirps
    chirp_dt  = cfg.n_frame / cfg.sample_rate
    raw_leak  = int(round(cfg.signal_freq * chirp_dt * n_dop))
    leak_wrap = raw_leak % n_dop
    sep_bins  = 2.0 * cfg.chirp_bw * 2.0 / 3e8
    frac_err  = abs(cfg.ramp_time_s - chirp_dt) * cfg.sample_rate
    print(
        f"[v4] bw={cfg.chirp_bw/1e6:.0f} MHz  ramp={int(cfg.ramp_time_s*1e6)} µs  "
        f"chirps={n_dop}  n_frame={cfg.n_frame}  fs={cfg.sample_rate/1e6:.3f} MHz\n"
        f"[v4] chirp_dt={chirp_dt*1e6:.3f} µs  frac_err={frac_err:.2f} samp/chirp  "
        f"(0.00 = perfect integer alignment)\n"
        f"[v4] R_res={cfg.range_res_m:.3f} m  v_res={cfg.vel_res_m_s:.3f} m/s  "
        f"v_max=±{cfg.max_vel_m_s:.1f} m/s\n"
        f"[v4] sep_bins@2m={sep_bins:.1f}  "
        f"theory_leak=±{leak_wrap} bins from DC  "
        f"({'AT DC!' if leak_wrap == 0 else 'at Nyquist edge — OK' if leak_wrap == n_dop//2 else 'OK'})"
    )

    app    = QApplication(sys.argv)
    _dark_palette(app)
    win    = SentinelWindow(backend, cfg, args)
    worker = RxWorker(backend)
    win._worker = worker
    worker.data_ready.connect(win.on_capture)
    worker.error.connect(lambda m: print(f"[RX] {m}"))
    worker.start()
    win.show()
    ret = app.exec()
    worker.requestInterruption(); worker.wait(3000)
    try: backend.close()
    except Exception: pass
    return ret


if __name__ == "__main__":
    sys.exit(main())
