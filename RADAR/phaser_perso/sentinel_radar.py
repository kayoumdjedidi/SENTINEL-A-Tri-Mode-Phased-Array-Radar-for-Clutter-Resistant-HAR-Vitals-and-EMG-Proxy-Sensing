#!/usr/bin/env python3
"""SENTINEL Mode A — FMCW Range-Doppler + Micro-Doppler.

Full production script with PyQt5 / pyqtgraph GUI.
Builds on radar_dsp.py + radar_backends.py for hardware-neutral DSP.

SENTINEL competition spec parameters (defaults)
------------------------------------------------
  chirp_BW     = 200 MHz   →  R_res = 0.75 m
  ramp_time_us = 200 µs    →  PRF   = 5 kHz
  num_chirps   = 256        →  CPI   = 51.2 ms,  v_res = 0.29 m/s
  gain_taper   = Chebyshev  [8, 34, 84, 127, 127, 84, 34, 8]  (−30 dB SL)
  tx_gain      = 0          →  max TX power
  rx_gain      = 70         →  max RX gain

Usage
-----
  python sentinel_radar.py
  python sentinel_radar.py --sdr-uri ip:192.168.2.1   # direct Pluto (Pi only)
  python sentinel_radar.py --bw 500e6 --ramp 500       # high-BW experiment
"""
from __future__ import annotations

import argparse
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
from radar_backends import CaptureResult, ContinuousSawtoothBackend, HardwareParams
from target_detection_dbfs import cfar as _cfar

# ---------------------------------------------------------------------------
# pyqtgraph global config — set before any widget creation
# ---------------------------------------------------------------------------
pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
pg.setConfigOption("background", "#1a1a2e")
pg.setConfigOption("foreground", "#dddddd")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
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
        prog="sentinel_radar",
        description="SENTINEL Mode A — FMCW Range-Doppler + Micro-Doppler",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rpi-uri", default="ip:phaser.local",
                   help="Phaser IIO URI")
    p.add_argument("--sdr-uri", default="ip:phaser.local:50901",
                   help="Pluto SDR IIO URI")
    p.add_argument("--bw", type=float, default=200e6, metavar="Hz",
                   help="Chirp bandwidth (SENTINEL spec = 200e6)")
    p.add_argument("--ramp", type=int, default=200, metavar="us",
                   help="Ramp time in µs (SENTINEL spec = 200)")
    p.add_argument("--chirps", type=int, default=256,
                   help="Chirps per frame")
    p.add_argument("--output-freq", type=float, default=10.25e9, metavar="Hz",
                   help="RF output frequency")
    p.add_argument("--rx-gain", type=int, default=70,
                   help="RX gain dB (−3 to 70)")
    p.add_argument("--history-len", type=int, default=200,
                   help="Micro-Doppler history depth in frames")
    p.add_argument("--save-dir", type=Path, default=Path("recordings"),
                   metavar="DIR", help="Base directory for session recordings")
    return p


# ---------------------------------------------------------------------------
# RX acquisition worker — runs in its own QThread so rx() never blocks the GUI
# ---------------------------------------------------------------------------

class RxWorker(QThread):
    data_ready = pyqtSignal(object)   # emits CaptureResult
    error      = pyqtSignal(str)

    def __init__(self, backend: ContinuousSawtoothBackend) -> None:
        super().__init__()
        self._backend = backend

    def run(self) -> None:
        # Flush stale DMA buffers accumulated during connect()
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

    def __init__(
        self,
        backend: ContinuousSawtoothBackend,
        cfg,
        args: argparse.Namespace,
    ) -> None:
        super().__init__()
        self._backend  = backend
        self._cfg      = cfg
        self._args     = args

        # Full range axis (includes negative range bins)
        self._range_m  = cfg.range_axis()
        self._vel_ms   = cfg.velocity_axis()

        # Positive-only mask — only these bins are shown in plots
        self._pos_mask  = self._range_m >= 0.0
        self._range_pos = self._range_m[self._pos_mask]

        # Last processed RD (positive-range slice) for auto-level
        self._last_rd: np.ndarray | None = None

        # Micro-Doppler rolling history
        self._history  = MicroDopplerHistory(
            history_len=args.history_len,
            doppler_bins=cfg.num_chirps,
        )

        # Recording state
        self._saving       = False
        self._raw_bursts:  list[np.ndarray] = []
        self._raw_ch0:     list[np.ndarray] = []
        self._raw_ch1:     list[np.ndarray] = []
        self._spectrograms:list[np.ndarray] = []
        self._timestamps:  list[float]      = []

        self.setWindowTitle(
            f"SENTINEL Mode A  |  {cfg.chirp_bw/1e6:.0f} MHz  "
            f"{int(cfg.ramp_time_s*1e6)} µs  {cfg.num_chirps} chirps  "
            f"R_res={cfg.range_res_m:.2f} m  v_res={cfg.vel_res_m_s:.2f} m/s"
        )
        self._build_ui()
        self.showMaximized()

    # ------------------------------------------------------------------
    # UI construction
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
        box = QGroupBox("SENTINEL Controls")
        box.setFixedWidth(250)
        lay = QVBoxLayout(box)
        lay.setSpacing(4)

        def lbl(text: str, small: bool = False) -> QLabel:
            w = QLabel(text)
            w.setStyleSheet(f"font-size: {'10' if small else '11'}px; color: #ccc;")
            return w

        def hslider(lo: int, hi: int, val: int, tick: int = 0) -> QSlider:
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(val)
            if tick:
                s.setTickInterval(tick)
                s.setTickPosition(QSlider.TicksBelow)
            return s

        # ── Beam steering ──────────────────────────────────────────────
        lay.addWidget(lbl("Beam Steering (°)"))
        self._steer_sl  = hslider(-80, 80, 0, tick=20)
        self._steer_lbl = lbl("0°", small=True)
        self._steer_sl.valueChanged.connect(self._on_steer)
        lay.addWidget(self._steer_sl)
        lay.addWidget(self._steer_lbl)

        # ── RX Gain ────────────────────────────────────────────────────
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

        # ── Bandwidth ──────────────────────────────────────────────────
        lay.addWidget(lbl("Bandwidth (MHz)"))
        self._bw_sl  = hslider(100, 500, int(self._cfg.chirp_bw / 1e6), tick=100)
        self._bw_lbl = lbl(
            f"{self._cfg.chirp_bw/1e6:.0f} MHz  "
            f"R_res={self._cfg.range_res_m:.2f} m",
            small=True,
        )
        self._bw_sl.valueChanged.connect(self._on_bw_slide)
        apply_bw = QPushButton("Apply BW")
        apply_bw.setFixedHeight(24)
        apply_bw.clicked.connect(self._on_bw_apply)
        lay.addWidget(self._bw_sl)
        lay.addWidget(self._bw_lbl)
        lay.addWidget(apply_bw)

        # ── Max display range ──────────────────────────────────────────
        lay.addWidget(lbl("Max Display Range (m)"))
        r_max_phys = float(self._range_pos.max()) if len(self._range_pos) else 30.0
        r_max_init = max(5, min(30, int(r_max_phys)))
        self._maxr_sl  = hslider(2, 100, r_max_init, tick=10)
        self._maxr_lbl = lbl(f"{r_max_init} m", small=True)
        self._maxr_sl.valueChanged.connect(self._on_max_range)
        lay.addWidget(self._maxr_sl)
        lay.addWidget(self._maxr_lbl)

        # ── DSP toggles ────────────────────────────────────────────────
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

        # ── CFAR bias ──────────────────────────────────────────────────
        lay.addWidget(lbl("CFAR bias (dB)"))
        self._cfar_sl  = hslider(0, 50, 15)
        self._cfar_lbl = lbl("15 dB", small=True)
        self._cfar_sl.valueChanged.connect(
            lambda v: self._cfar_lbl.setText(f"{v} dB")
        )
        lay.addWidget(self._cfar_sl)
        lay.addWidget(self._cfar_lbl)

        # ── Zero-range blanking width ──────────────────────────────────
        lay.addWidget(lbl("Zero-range blank (bins ±)"))
        self._zrw_sl  = hslider(0, 20, 2)
        self._zrw_lbl = lbl("±2 bins", small=True)
        self._zrw_sl.valueChanged.connect(self._on_zrw_slide)
        lay.addWidget(self._zrw_sl)
        lay.addWidget(self._zrw_lbl)

        # ── Leakage Doppler blank width ────────────────────────────────
        lay.addWidget(lbl("Leakage Doppler blank (cols ±)"))
        self._ldw_sl  = hslider(0, 12, 3)
        self._ldw_lbl = lbl("±3 cols", small=True)
        self._ldw_sl.valueChanged.connect(
            lambda v: self._ldw_lbl.setText(f"±{v} cols")
        )
        lay.addWidget(self._ldw_sl)
        lay.addWidget(self._ldw_lbl)

        # ── Color levels ───────────────────────────────────────────────
        lay.addWidget(lbl("Color floor (×0.1 log10)"))
        self._floor_sl  = hslider(0, 80, 20)
        self._floor_lbl = lbl("2.0", small=True)
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

        auto_lv = QPushButton("Auto Level")
        auto_lv.setFixedHeight(24)
        auto_lv.clicked.connect(self._on_auto_level)
        lay.addWidget(auto_lv)

        # ── Recording ──────────────────────────────────────────────────
        self._save_cb  = QCheckBox("Record session")
        self._save_cb.setChecked(False)
        self._save_cb.stateChanged.connect(self._on_save_toggle)
        lay.addWidget(self._save_cb)
        self._save_lbl = lbl("Not recording", small=True)
        self._save_lbl.setStyleSheet("font-size: 10px; color: #888;")
        lay.addWidget(self._save_lbl)

        lay.addStretch()

        # ── Status ─────────────────────────────────────────────────────
        self._status_lbl = QLabel("Waiting for first frame…")
        self._status_lbl.setStyleSheet("font-size: 10px; color: #88aaff;")
        self._status_lbl.setWordWrap(True)
        lay.addWidget(self._status_lbl)

        # ── Quit ───────────────────────────────────────────────────────
        quit_btn = QPushButton("Quit")
        quit_btn.setStyleSheet("background-color: #662222; color: white; font-weight: bold;")
        quit_btn.setFixedHeight(30)
        quit_btn.clicked.connect(self._on_quit)
        lay.addWidget(quit_btn)

        return box

    def _build_plots(self) -> pg.GraphicsLayoutWidget:
        glw = pg.GraphicsLayoutWidget()
        r   = self._range_pos          # positive range only
        v   = self._vel_ms
        h   = self._args.history_len

        r_display = float(self._maxr_sl.value())   # initial max display range

        label_kw = {"color": "#ccc", "font-size": "10pt"}

        # ── Row 0, col 0: Range profile (standard: x=range, y=power) ──
        p_rp = glw.addPlot(row=0, col=0, title="Range Profile")
        p_rp.setLabel("bottom", "Range",         units="m",   **label_kw)
        p_rp.setLabel("left",   "Power (log₁₀)",              **label_kw)
        p_rp.setXRange(0.0, r_display, padding=0)
        p_rp.enableAutoRange("xy", False)
        p_rp.showGrid(x=True, y=True, alpha=0.2)

        self._rp_curve = p_rp.plot(
            pen=pg.mkPen(color=(255, 200, 50), width=1.5),
        )
        self._cfar_curve = p_rp.plot(
            pen=pg.mkPen(color=(50, 200, 255), width=1.2, style=Qt.DashLine),
        )
        self._gate_line_rp = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=2),
            label="gate", labelOpts={"color": "#4f8", "position": 0.9},
        )
        p_rp.addItem(self._gate_line_rp)

        # ── Row 0, col 1: Range-Doppler heatmap ────────────────────────
        p_rd = glw.addPlot(row=0, col=1, title="Range-Doppler")
        p_rd.setLabel("left",   "Range",    units="m",   **label_kw)
        p_rd.setLabel("bottom", "Velocity", units="m/s", **label_kw)
        p_rd.setYRange(0.0, r_display, padding=0)
        p_rd.setXRange(float(v.min()), float(v.max()), padding=0)
        p_rd.enableAutoRange("xy", False)
        p_rd.showGrid(x=True, y=True, alpha=0.15)

        self._rd_img = pg.ImageItem()
        self._rd_img.setLookupTable(INFERNO_LUT)
        # Image covers positive range only: y goes from 0 to r.max()
        # row-major: image[row, col] → y=row, x=col
        # rd_pos shape (n_pos_range, n_doppler): rows=range(y), cols=doppler(x)
        v_span = float(v.max() - v.min())
        r_span = float(r.max()) if len(r) else 30.0
        self._rd_img.setRect(pg.QtCore.QRectF(
            float(v.min()), 0.0, v_span, r_span,
        ))
        p_rd.addItem(self._rd_img)

        self._gate_line_rd = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=1.5, style=Qt.DashLine),
        )
        p_rd.addItem(self._gate_line_rd)

        # ── Row 1, colspan 2: Micro-Doppler history ────────────────────
        p_md = glw.addPlot(row=1, col=0, colspan=2, title="Micro-Doppler History")
        p_md.setLabel("left",   "Velocity", units="m/s", **label_kw)
        p_md.setLabel("bottom", "Frame index",           **label_kw)
        p_md.setXRange(0, h, padding=0)
        p_md.setYRange(float(v.min()), float(v.max()), padding=0)
        p_md.enableAutoRange("xy", False)
        p_md.showGrid(x=True, y=True, alpha=0.15)

        self._md_img = pg.ImageItem()
        self._md_img.setLookupTable(VIRIDIS_LUT)
        # history shape (history_len, n_doppler); displayed as .T so:
        # rows=doppler(velocity), cols=time → y=velocity, x=time
        self._md_img.setRect(pg.QtCore.QRectF(
            0.0, float(v.min()), float(h), float(v.max() - v.min()),
        ))
        p_md.addItem(self._md_img)

        # Row height weights
        glw.ci.layout.setRowStretchFactor(0, 3)
        glw.ci.layout.setRowStretchFactor(1, 2)

        self._p_rp = p_rp
        self._p_rd = p_rd
        self._p_md = p_md
        return glw

    # ------------------------------------------------------------------
    # Data slot — called in the GUI thread via Qt signal/slot
    # ------------------------------------------------------------------

    def on_capture(self, cap: CaptureResult) -> None:
        t0  = time.monotonic()
        cfg = cap.cfg

        # ── 1. DSP pipeline ────────────────────────────────────────────
        result = process_frame(
            cap.chirp_matrix,
            apply_mti_filter=self._mti_cb.isChecked(),
            window=self._win_cb.isChecked(),
            range_axis_m=self._range_m,
            min_range_m=0.3,
            min_scale=None,
            max_scale=None,
        )
        rd = result["rd_map"].copy()  # (n_range_full, n_doppler) float32

        # ── 2. TX-leakage suppression (full range axis) ────────────────
        if self._zrs_cb.isChecked():
            rd_min = rd.min()
            zrw    = self._zrw_sl.value()

            # Range dimension: blank ±zrw bins around the 0-m range bin
            zero_bin = int(np.argmin(np.abs(self._range_m)))
            lo_r = max(0, zero_bin - zrw)
            hi_r = min(rd.shape[0], zero_bin + zrw)
            rd[lo_r:hi_r, :] = rd_min

            # Doppler dimension: blank predicted IF leakage columns
            # offset = round(f_IF * T_ramp * N_chirps)
            leakage_offset = int(round(
                cfg.signal_freq * cfg.ramp_time_s * cfg.num_chirps
            ))
            ldw = self._ldw_sl.value()
            if leakage_offset > 0 and ldw > 0:
                n_dop = rd.shape[1]
                half  = n_dop // 2
                for sign in (+1, -1):
                    col_c = half + sign * leakage_offset
                    lo_c  = max(0,     col_c - ldw)
                    hi_c  = min(n_dop, col_c + ldw + 1)
                    rd[:, lo_c:hi_c] = rd_min

        # ── 3. Clip to positive range only ─────────────────────────────
        rd_pos      = rd[self._pos_mask, :]          # (n_pos, n_doppler)
        range_pos   = self._range_pos                # metres
        self._last_rd = rd_pos

        # ── 4. Range profile (peak Doppler per range bin) ──────────────
        profile  = rd_pos.max(axis=1)                # (n_pos,)
        sel_bin  = result["range_bin"]
        # Remap sel_bin from full range axis to positive-only axis
        # Find closest positive-range bin
        sel_bin_pos = int(np.argmin(np.abs(range_pos - self._range_m[sel_bin]))) \
            if sel_bin < len(self._range_m) else 0

        # ── 5. CFAR — refine range gate (on positive-range profile) ────
        # Use small guard/ref so CFAR works close in (3 guard + 8 ref = 11 min bins)
        cfar_thresh_arr = None
        if self._cfar_cb.isChecked():
            try:
                bias = float(self._cfar_sl.value())
                thresh, _ = _cfar(
                    profile,
                    num_guard_cells=3,
                    num_ref_cells=8,
                    bias=bias,
                )
                cfar_thresh_arr = np.ma.filled(thresh, profile.min())
                # Only look in unblocked positive range
                blank_m = self._zrw_sl.value() * (
                    self._cfg.range_res_m if hasattr(self._cfg, "range_res_m") else 0.75
                )
                min_r = max(0.3, blank_m + self._cfg.range_res_m)
                pos_mask2 = range_pos >= min_r
                above = np.flatnonzero(
                    pos_mask2 & (profile > cfar_thresh_arr)
                )
                if above.size:
                    sel_bin_pos = int(above[np.argmax(profile[above])])
            except Exception:
                pass

        range_m = float(range_pos[sel_bin_pos]) if sel_bin_pos < len(range_pos) else 0.0

        # ── 6. Micro-Doppler slice + history ───────────────────────────
        md_slice = rd_pos[sel_bin_pos, :].astype(np.float32)
        history  = self._history.push(md_slice)      # (history_len, n_doppler)

        # ── 7. Display levels ──────────────────────────────────────────
        floor = self._floor_sl.value() / 10.0
        ceil  = self._ceil_sl.value()  / 10.0
        if ceil <= floor:
            ceil = floor + 0.1

        # ── 8. Update plots ────────────────────────────────────────────
        # Range profile (x=range, y=power) — standard orientation
        self._rp_curve.setData(x=range_pos, y=profile)
        if cfar_thresh_arr is not None:
            self._cfar_curve.setData(x=range_pos, y=cfar_thresh_arr)
        self._gate_line_rp.setValue(range_m)
        self._gate_line_rd.setValue(range_m)

        # Range-Doppler — rows=range (y), cols=doppler (x), positive range only
        self._rd_img.setImage(rd_pos, levels=(floor, ceil))

        # Micro-Doppler — .T so rows=velocity (y), cols=time (x)
        self._md_img.setImage(history.T, levels=(floor, ceil))

        # ── 9. Recording ───────────────────────────────────────────────
        if self._saving:
            self._raw_bursts.append(cap.chirp_matrix.copy())
            self._spectrograms.append(md_slice.copy())
            self._timestamps.append(t0)
            if cap.ch0 is not None:
                self._raw_ch0.append(cap.ch0.copy())
            if cap.ch1 is not None:
                self._raw_ch1.append(cap.ch1.copy())

        # ── 10. Status bar ─────────────────────────────────────────────
        dt       = (time.monotonic() - t0) * 1000
        n        = len(self._raw_bursts) if self._saving else 0
        blank_m  = self._zrw_sl.value() * cfg.range_res_m
        self._status_lbl.setText(
            f"gate: {range_m:.2f} m  bin: {sel_bin_pos}\n"
            f"blanked: 0–{blank_m:.1f} m\n"
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

    def _on_bw_slide(self, value: int) -> None:
        bw    = value * 1e6
        r_res = 3e8 / (2.0 * bw)
        self._bw_lbl.setText(f"{value} MHz  R_res={r_res:.2f} m")

    def _on_bw_apply(self) -> None:
        """Push new bandwidth to the ADF4159 PLL and update all derived state."""
        bw        = self._bw_sl.value() * 1e6
        num_steps = int(self._cfg.ramp_time_s * 1e6)   # 1 step/µs
        try:
            ph = self._backend._phaser
            ph.freq_dev_range = int(bw / 4)
            ph.freq_dev_step  = int((bw / 4) / max(num_steps, 1))
            ph.enable = 0
            print(f"[BW] applied {bw/1e6:.0f} MHz")
        except Exception as exc:
            print(f"[BW] failed: {exc}")
            return

        # Update config and all derived axes
        self._cfg      = replace(self._cfg, chirp_bw=bw)
        self._range_m  = self._cfg.range_axis()
        self._pos_mask  = self._range_m >= 0.0
        self._range_pos = self._range_m[self._pos_mask]

        r   = self._range_pos
        v   = self._vel_ms
        r_display = float(self._maxr_sl.value())

        self._p_rp.setXRange(0.0, r_display, padding=0)
        self._p_rd.setYRange(0.0, r_display, padding=0)
        self._rd_img.setRect(pg.QtCore.QRectF(
            float(v.min()), 0.0,
            float(v.max() - v.min()),
            float(r.max()) if len(r) else 30.0,
        ))

        cfg = self._cfg
        self.setWindowTitle(
            f"SENTINEL Mode A  |  {cfg.chirp_bw/1e6:.0f} MHz  "
            f"{int(cfg.ramp_time_s*1e6)} µs  {cfg.num_chirps} chirps  "
            f"R_res={cfg.range_res_m:.2f} m  v_res={cfg.vel_res_m_s:.2f} m/s"
        )

    def _on_max_range(self, value: int) -> None:
        self._maxr_lbl.setText(f"{value} m")
        self._p_rp.setXRange(0.0, float(value), padding=0)   # range profile: X = range
        self._p_rd.setYRange(0.0, float(value), padding=0)   # RD map: Y = range

    def _on_zrw_slide(self, value: int) -> None:
        r_res = self._cfg.range_res_m if hasattr(self._cfg, "range_res_m") else 0.75
        blank_m = value * r_res
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
                metadata={
                    "mti":        self._mti_cb.isChecked(),
                    "window":     self._win_cb.isChecked(),
                    "cfar":       self._cfar_cb.isChecked(),
                    "cfar_bias":  self._cfar_sl.value(),
                    "zrs":        self._zrs_cb.isChecked(),
                },
            )
            if self._raw_ch0:
                np.save(sdir / "raw_ch0.npy",
                        np.stack(self._raw_ch0, axis=0).astype(np.complex64))
            if self._raw_ch1:
                np.save(sdir / "raw_ch1.npy",
                        np.stack(self._raw_ch1, axis=0).astype(np.complex64))
            np.save(sdir / "timestamps.npy",
                    np.array(self._timestamps, dtype=np.float64))

            n = len(self._raw_bursts)
            print(f"[sentinel_radar] Saved {n} frames → {sdir}")
            self._save_lbl.setText(f"Saved {n} frames")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #88ff88;")
        except Exception as exc:
            print(f"[sentinel_radar] Save failed: {exc}")
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
    pal = QPalette()
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
    parser = build_parser()
    args   = parser.parse_args(argv)

    print("[sentinel_radar] Connecting to hardware…")
    hw = HardwareParams(
        rpi_uri      = args.rpi_uri,
        sdr_uri      = args.sdr_uri,
        sample_rate  = 4e6,
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
    backend = ContinuousSawtoothBackend(hw)
    cfg     = backend.connect()

    print(
        f"[sentinel_radar] "
        f"rf={cfg.output_freq/1e9:.4f} GHz  "
        f"bw={cfg.chirp_bw/1e6:.0f} MHz  "
        f"ramp={int(cfg.ramp_time_s*1e6)} µs  "
        f"chirps={cfg.num_chirps}  "
        f"spc={cfg.n_frame}  "
        f"R_res={cfg.range_res_m:.3f} m  "
        f"v_res={cfg.vel_res_m_s:.3f} m/s  "
        f"max_vel=±{cfg.max_vel_m_s:.1f} m/s"
    )

    app = QApplication(sys.argv)
    _dark_palette(app)

    win         = SentinelWindow(backend, cfg, args)
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
