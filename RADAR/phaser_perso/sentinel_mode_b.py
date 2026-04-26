"""SENTINEL Mode B — CW Continuous Vitals Monitor.

Radiates a fixed CW tone (no chirp) and extracts respiration and heartbeat
by tracking the micro-Doppler phase shift of the reflected signal.

Architecture
------------
  cw_dsp.py           CWProcessor — mix, decimate, phase demod, BPF, BPM
  sentinel_mode_b.py  hardware bring-up + QThread worker + PyQt5/pyqtgraph GUI

Hardware topology (Windows, proven)
------------------------------------
  PC → Pluto  via  ip:192.168.2.1      RX0+RX1 both enabled (AD9361 requirement)
  PC → Phaser via  ip:phaser.local     ADF4159 CW (ramp disabled)

  TX1 driven with cyclic 100 kHz tone (N=2^18).  TX0 muted at -88 dB.
  rx_buffer_size set AFTER sdr.tx([iq,iq]) — matches radar_backends.py.
  Do NOT use ip:phaser.local:50901 — ETIMEDOUT on Windows (confirmed).

DSP
---
  IF IQ @ 600 kHz  →  mix to baseband (phase-continuous across frames)
  →  decimate ÷ 6000  →  ~100 Hz phase stream (20 s ring buffer)
  →  detrend  →  BPF resp [0.15–0.5 Hz]  +  BPF HR [0.8–2.5 Hz]
  →  displacement (mm)  +  Welch BPM estimate + confidence

Heartbeat interpretation
------------------------
  This is radar phase micro-motion, not ECG.  Chest/skin displacement d changes
  round-trip phase by 4πd/λ; at 10.25 GHz, λ≈29 mm, so sub-mm motion is visible.
  The HR readout is the strongest sufficiently confident periodic motion in the
  0.8–2.5 Hz band.  Respiration harmonics, body motion, cable movement, and
  multipath can create false HR; validate against a pulse ox/watch/ECG.

GUI panels
----------
  Top-left   : IF FFT  — zoomed ±2 kHz around 100 kHz tone
  Top-right  : IF waterfall  — time-frequency view of the same band
  Bottom-left: Displacement trace  — 20 s rolling, resp (cyan) + HR (magenta)
  Bottom-right: Phase PSD  — 0–4 Hz with resp/HR bands marked and BPM lines

Run
---
  python sentinel_mode_b.py
  python sentinel_mode_b.py --rx-gain 55 --output-freq 10.1e9
  python sentinel_mode_b.py --history-sec 30 --no-record
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
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

from cw_dsp import CWProcessor, VitalsResult


# ---------------------------------------------------------------------------
# pyqtgraph global config (dark theme, no SI unit rescaling)
# ---------------------------------------------------------------------------
pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
pg.setConfigOption("background", "#1a1a2e")
pg.setConfigOption("foreground", "#dddddd")

LABEL_KW = {"color": "#ccc", "font-size": "10pt"}

CHEBYSHEV_TAPER = (8, 34, 84, 127, 127, 84, 34, 8)

# Viridis-style LUT for the waterfall
def _viridis_lut() -> np.ndarray:
    try:
        cmap = pg.colormap.get("viridis", source="matplotlib")
    except Exception:
        cmap = pg.colormap.get("viridis")
    return cmap.getLookupTable(0.0, 1.0, 256)

VIRIDIS_LUT = _viridis_lut()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel_mode_b",
        description="SENTINEL Mode B: CW continuous vitals monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    hw = p.add_argument_group("hardware URIs")
    hw.add_argument("--rpi-uri", default="ip:phaser.local")
    hw.add_argument("--sdr-uri", default="ip:192.168.2.1",
                    help="Pluto IIO URI. Do NOT use ip:phaser.local:50901 (ETIMEDOUT).")

    r = p.add_argument_group("RF / CW tuning")
    r.add_argument("--output-freq", type=float, default=10.25e9,
                   help="Radiated RF frequency (Hz)")
    r.add_argument("--center-freq", type=float, default=2.1e9,
                   help="Pluto IF / LO (Hz)")
    r.add_argument("--signal-freq", type=float, default=100e3,
                   help="CW IF tone frequency (Hz)")
    r.add_argument("--sample-rate", type=float, default=600e3,
                   help="Pluto ADC/DAC sample rate (Hz)")
    r.add_argument("--frame-size", type=int, default=8192,
                   help="Samples per rx() call")
    r.add_argument("--rx-gain", type=int, default=50,
                   help="RX0/RX1 hardware gain (dB, -3 to 70)")
    r.add_argument("--tx-gain", type=int, default=-6,
                   help="TX1 hardware gain (dB, -88 to 0)")
    r.add_argument("--gain-taper",
                   default=",".join(str(g) for g in CHEBYSHEV_TAPER),
                   metavar="g0,..,g7",
                   help="8 ADAR1000 channel gains (0-127)")
    r.add_argument("--steer", type=float, default=0.0, metavar="DEG")

    d = p.add_argument_group("DSP / display")
    d.add_argument("--history-sec", type=float, default=20.0,
                   help="Phase history window length (s)")
    d.add_argument("--phase-fs", type=float, default=100.0,
                   help="Target phase sample rate after decimation (Hz)")
    d.add_argument("--vitals-warmup-sec", type=float, default=8.0,
                   help="Phase history required before showing vitals")
    d.add_argument("--min-resp-conf", type=float, default=0.05,
                   help="Minimum confidence before showing RR")
    d.add_argument("--min-hr-conf", type=float, default=0.12,
                   help="Minimum confidence before showing HR")
    d.add_argument("--waterfall-depth", type=int, default=60,
                   help="Number of IF FFT rows in waterfall")

    s = p.add_argument_group("recording")
    s.add_argument("--save-dir", type=Path, default=Path("recordings"),
                   help="Root directory for session recordings")
    s.add_argument("--no-record", action="store_true",
                   help="Disable recording UI (display only)")
    return p


# ---------------------------------------------------------------------------
# Acquisition worker
# ---------------------------------------------------------------------------

class CWWorker(QThread):
    """Runs sdr.rx() in a background thread; emits each raw IQ frame."""

    frame_ready = pyqtSignal(object)   # numpy complex64 array
    error       = pyqtSignal(str)

    def __init__(self, sdr) -> None:
        super().__init__()
        self._sdr = sdr

    def run(self) -> None:
        consec_errors = 0
        while not self.isInterruptionRequested():
            try:
                raw = self._sdr.rx()
                # Two-channel rx() returns a list; sum coherently for SNR gain
                if isinstance(raw, (list, tuple)):
                    arr = (np.asarray(raw[0], dtype=np.complex64) +
                           np.asarray(raw[1], dtype=np.complex64))
                elif hasattr(raw, "ndim") and raw.ndim > 1:
                    arr = raw[0].astype(np.complex64) + raw[1].astype(np.complex64)
                else:
                    arr = np.asarray(raw, dtype=np.complex64).ravel()
                self.frame_ready.emit(arr)
                consec_errors = 0
            except Exception as exc:
                consec_errors += 1
                self.error.emit(str(exc))
                if consec_errors >= 5:
                    break   # give up after 5 consecutive failures
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ModeBWindow(QMainWindow):

    _WF_SPAN_HZ  = 2000.0   # ±Hz around tone for waterfall / FFT zoom
    _FFT_DB_MIN  = -110.0
    _FFT_DB_MAX  = 0.0

    def __init__(self, sdr, phaser, args: argparse.Namespace) -> None:
        super().__init__()
        self._sdr     = sdr
        self._phaser  = phaser
        self._args    = args

        self._proc = CWProcessor(
            fs          = args.sample_rate,
            signal_freq = args.signal_freq,
            rf_freq     = args.output_freq,
            history_sec = args.history_sec,
            phase_fs    = args.phase_fs,
            warmup_sec  = args.vitals_warmup_sec,
        )

        # IF FFT axis
        fs   = args.sample_rate
        n    = args.frame_size
        self._freq_axis = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
        self._win_fft   = np.blackman(n).astype(np.float32)
        self._win_scale = float(np.sum(self._win_fft))

        # Waterfall buffer. Store only the displayed IF band as
        # (frequency_bin, frame_history) so the image axes stay physical.
        self._wf_depth = args.waterfall_depth
        self._wf_mask = (
            (self._freq_axis >= args.signal_freq - self._WF_SPAN_HZ)
            & (self._freq_axis <= args.signal_freq + self._WF_SPAN_HZ)
        )
        if not np.any(self._wf_mask):
            self._wf_mask = np.ones_like(self._freq_axis, dtype=bool)
        self._wf_bins = int(np.count_nonzero(self._wf_mask))
        self._wf_buf = np.full(
            (self._wf_bins, self._wf_depth), self._FFT_DB_MIN, dtype=np.float32
        )

        # Recording state
        self._saving    = False
        self._rec_iq:   list[np.ndarray] = []
        self._rec_ts:   list[float]      = []
        self._rec_vitals: list[dict]     = []

        self._last_vitals: VitalsResult | None = None
        self._frame_count  = 0

        self.setWindowTitle(
            f"SENTINEL Mode B  [CW]  "
            f"RF={args.output_freq/1e9:.3f} GHz  "
            f"SR={args.sample_rate/1e3:.0f} kHz  "
            f"tone={args.signal_freq/1e3:.0f} kHz"
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
        box = QGroupBox("Mode B Controls")
        box.setFixedWidth(240)
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

        # Waterfall levels
        lay.addWidget(lbl("WF floor (dB)"))
        self._wf_floor_sl  = slider(-110, -20, -100, 10)
        self._wf_floor_lbl = lbl("-100 dB", True)
        self._wf_floor_sl.valueChanged.connect(
            lambda v: self._wf_floor_lbl.setText(f"{v} dB"))
        lay.addWidget(self._wf_floor_sl)
        lay.addWidget(self._wf_floor_lbl)

        lay.addWidget(lbl("WF ceiling (dB)"))
        self._wf_ceil_sl  = slider(-80, 0, -30, 10)
        self._wf_ceil_lbl = lbl("-30 dB", True)
        self._wf_ceil_sl.valueChanged.connect(
            lambda v: self._wf_ceil_lbl.setText(f"{v} dB"))
        lay.addWidget(self._wf_ceil_sl)
        lay.addWidget(self._wf_ceil_lbl)

        # BPM readouts (large, colored)
        lay.addWidget(lbl("────── Vitals ──────"))
        self._rr_lbl = QLabel("RR: –– bpm")
        self._rr_lbl.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #00ffcc;")
        lay.addWidget(self._rr_lbl)

        self._rr_conf_lbl = lbl("Resp conf: –", True)
        lay.addWidget(self._rr_conf_lbl)

        self._hr_lbl = QLabel("HR: –– bpm")
        self._hr_lbl.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #ff66cc;")
        lay.addWidget(self._hr_lbl)

        self._hr_conf_lbl = lbl("Heart conf: –", True)
        lay.addWidget(self._hr_conf_lbl)

        # Recording
        if not self._args.no_record:
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
        sig = self._args.signal_freq
        span = self._WF_SPAN_HZ

        # ── IF FFT (top-left) ─────────────────────────────────────────
        p_fft = glw.addPlot(row=0, col=0, title="IF Spectrum  (dBFS)")
        p_fft.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_fft.setLabel("left",   "dBFS",           **LABEL_KW)
        p_fft.setXRange(sig - span, sig + span, padding=0)
        p_fft.setYRange(self._FFT_DB_MIN, self._FFT_DB_MAX, padding=0)
        p_fft.enableAutoRange("xy", False)
        p_fft.showGrid(x=True, y=True, alpha=0.2)
        # Tone marker
        tone_line = pg.InfiniteLine(
            pos=sig, angle=90, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=1, style=Qt.DashLine),
        )
        p_fft.addItem(tone_line)
        self._fft_curve = p_fft.plot(pen=pg.mkPen(color=(255, 200, 50), width=1.5))

        # ── IF Waterfall (top-right) ───────────────────────────────────
        p_wf = glw.addPlot(row=0, col=1, title="IF Waterfall")
        p_wf.setLabel("left",   "Frequency [Hz]",  **LABEL_KW)
        p_wf.setLabel("bottom", "Frame history",    **LABEL_KW)
        p_wf.setXRange(0, self._wf_depth, padding=0)
        p_wf.setYRange(sig - span, sig + span, padding=0)
        p_wf.enableAutoRange("xy", False)

        self._wf_img = pg.ImageItem()
        self._wf_img.setLookupTable(VIRIDIS_LUT)
        self._wf_img.setRect(pg.QtCore.QRectF(
            0.0, sig - span, float(self._wf_depth), 2.0 * span))
        p_wf.addItem(self._wf_img)

        # ── Displacement trace (bottom-left) ──────────────────────────
        p_disp = glw.addPlot(row=1, col=0,
                             title=f"Phase Displacement  ({self._args.history_sec:g} s window)")
        p_disp.setLabel("bottom", "Time [s]",           **LABEL_KW)
        p_disp.setLabel("left",   "Displacement [mm]",  **LABEL_KW)
        p_disp.setXRange(0.0, self._args.history_sec, padding=0)
        p_disp.setYRange(-2.0, 2.0, padding=0)
        p_disp.enableAutoRange("y", True)
        p_disp.enableAutoRange("x", False)
        p_disp.showGrid(x=True, y=True, alpha=0.2)

        self._resp_curve = p_disp.plot(
            pen=pg.mkPen(color=(0, 255, 200), width=1.5),
            name="Respiration",
        )
        self._hr_curve = p_disp.plot(
            pen=pg.mkPen(color=(255, 100, 200), width=1.5),
            name="Heartbeat",
        )
        p_disp.addLegend(offset=(-10, 10))

        # ── Phase PSD (bottom-right) ───────────────────────────────────
        p_psd = glw.addPlot(row=1, col=1, title="Phase PSD  (0–4 Hz)")
        p_psd.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_psd.setLabel("left",   "PSD [dB/Hz]",    **LABEL_KW)
        p_psd.setXRange(0.0, 4.0, padding=0)
        p_psd.enableAutoRange("y", True)
        p_psd.enableAutoRange("x", False)
        p_psd.showGrid(x=True, y=True, alpha=0.2)

        # Shaded band regions
        resp_region = pg.LinearRegionItem(
            [0.15, 0.50], movable=False,
            brush=pg.mkBrush(0, 255, 200, 30),
            pen=pg.mkPen(color=(0, 255, 200), width=0.5),
        )
        hr_region = pg.LinearRegionItem(
            [0.80, 2.50], movable=False,
            brush=pg.mkBrush(255, 100, 200, 30),
            pen=pg.mkPen(color=(255, 100, 200), width=0.5),
        )
        p_psd.addItem(resp_region)
        p_psd.addItem(hr_region)

        self._psd_curve = p_psd.plot(
            pen=pg.mkPen(color=(255, 200, 50), width=1.5))

        # BPM marker lines (start invisible)
        self._resp_bpm_line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(color=(0, 255, 200), width=2),
        )
        self._hr_bpm_line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(color=(255, 100, 200), width=2),
        )
        p_psd.addItem(self._resp_bpm_line)
        p_psd.addItem(self._hr_bpm_line)

        glw.ci.layout.setRowStretchFactor(0, 2)
        glw.ci.layout.setRowStretchFactor(1, 3)

        self._p_fft  = p_fft
        self._p_wf   = p_wf
        self._p_disp = p_disp
        self._p_psd  = p_psd

        return glw

    # ------------------------------------------------------------------
    # Frame processing slot (GUI thread)
    # ------------------------------------------------------------------

    def on_frame(self, raw_iq: np.ndarray) -> None:
        t0 = time.monotonic()
        self._frame_count += 1
        n = len(raw_iq)

        # 1. IF FFT for display
        sig  = self._args.signal_freq
        data = raw_iq[:len(self._win_fft)] if n >= len(self._win_fft) else (
            np.pad(raw_iq, (0, len(self._win_fft) - n)))
        sp   = np.fft.fftshift(np.fft.fft(data * self._win_fft))
        s_db = (20.0 * np.log10(
            np.maximum(np.abs(sp) / self._win_scale / 2.0**11, 1e-15)
        )).astype(np.float32)

        self._fft_curve.setData(self._freq_axis, s_db)

        # 2. Waterfall (roll left, insert newest frame on the right)
        self._wf_buf = np.roll(self._wf_buf, -1, axis=1)
        self._wf_buf[:, -1] = s_db[self._wf_mask]
        self._wf_img.setImage(
            self._wf_buf,
            levels=(float(self._wf_floor_sl.value()),
                    float(self._wf_ceil_sl.value())),
            autoLevels=False,
        )

        # 3. Push raw IQ into CWProcessor
        self._proc.push(raw_iq)

        # 4. Compute vitals (not every frame — throttle to ~5 Hz)
        if self._frame_count % 15 == 0:
            result = self._proc.compute()
            if result is not None:
                self._last_vitals = result
                self._update_vitals_panels(result)

        # 5. Recording
        if self._saving:
            self._rec_iq.append(raw_iq.copy())
            self._rec_ts.append(t0)
            # Save a vitals snapshot every ~1 s (~70 frames)
            if self._last_vitals is not None and self._frame_count % 70 == 0:
                self._rec_vitals.append({
                    "t":         float(t0),
                    "resp_bpm":  float(self._last_vitals.resp_bpm),
                    "hr_bpm":    float(self._last_vitals.hr_bpm),
                    "resp_conf": float(self._last_vitals.resp_conf),
                    "hr_conf":   float(self._last_vitals.hr_conf),
                })

        # 6. Status
        dt_ms = (time.monotonic() - t0) * 1000
        self._status_lbl.setText(
            f"frame: {self._frame_count}  proc: {dt_ms:.0f} ms\n"
            f"saved: {len(self._rec_iq)}"
        )

    def _update_vitals_panels(self, r: VitalsResult) -> None:
        # Displacement trace
        if len(r.resp_disp_mm) > 1:
            self._resp_curve.setData(x=r.time_axis, y=r.resp_disp_mm)
            self._hr_curve.setData(  x=r.time_axis, y=r.hr_disp_mm)

        # PSD
        if len(r.psd_freq_hz) > 1:
            self._psd_curve.setData(x=r.psd_freq_hz, y=r.psd_db)

        # BPM markers on PSD panel
        if r.resp_bpm > 0:
            self._resp_bpm_line.setValue(r.resp_bpm / 60.0)
        if r.hr_bpm > 0:
            self._hr_bpm_line.setValue(r.hr_bpm / 60.0)

        # Text readouts
        if r.resp_conf >= float(self._args.min_resp_conf):
            self._rr_lbl.setText(f"RR: {r.resp_bpm:.1f} bpm")
        else:
            self._rr_lbl.setText("RR: –– bpm")
        if r.hr_conf >= float(self._args.min_hr_conf):
            self._hr_lbl.setText(f"HR: {r.hr_bpm:.1f} bpm")
        else:
            self._hr_lbl.setText("HR: –– bpm")

        conf_bar = lambda c: "▮" * int(c * 10) + "▯" * (10 - int(c * 10))
        self._rr_conf_lbl.setText(f"Resp conf: {conf_bar(r.resp_conf)}")
        self._hr_conf_lbl.setText(f"HR   conf: {conf_bar(r.hr_conf)}")

    # ------------------------------------------------------------------
    # Control callbacks
    # ------------------------------------------------------------------

    def _on_steer(self, value: int) -> None:
        self._steer_lbl.setText(f"{value}°")
        try:
            angle_rad   = np.radians(float(value))
            phase_delta = (
                2.0 * np.pi * self._args.output_freq * 0.014
                * np.sin(angle_rad) / 3e8
            )
            self._phaser.set_beam_phase_diff(np.degrees(phase_delta))
        except Exception as exc:
            print(f"[steer] {exc}")

    def _on_rx_gain_apply(self) -> None:
        gain = self._rxgain_sl.value()
        self._rxgain_lbl.setText(f"{gain} dB")
        try:
            self._sdr.rx_hardwaregain_chan0 = int(gain)
            self._sdr.rx_hardwaregain_chan1 = int(gain)
            print(f"[RX gain] {gain} dB applied (both channels)")
        except Exception as exc:
            print(f"[RX gain] {exc}")

    def _on_save_toggle(self, state: int) -> None:
        self._saving = (state == Qt.Checked)
        if self._saving:
            self._rec_iq.clear()
            self._rec_ts.clear()
            self._rec_vitals.clear()
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
            name = datetime.now().strftime("%Y%m%d_%H%M%S")
            sdir = base / name
            sdir.mkdir()

            a = self._args
            cfg = {
                "mode":        "B_CW",
                "output_freq": a.output_freq,
                "center_freq": a.center_freq,
                "signal_freq": a.signal_freq,
                "sample_rate": a.sample_rate,
                "frame_size":  a.frame_size,
                "rx_gain":     a.rx_gain,
                "tx_gain":     a.tx_gain,
                "history_sec": a.history_sec,
                "phase_fs":    a.phase_fs,
                "actual_phase_fs": self._proc.phase_fs,
                "vitals_warmup_sec": a.vitals_warmup_sec,
                "min_resp_conf": a.min_resp_conf,
                "min_hr_conf": a.min_hr_conf,
                "sdr_uri":     a.sdr_uri,
                "rpi_uri":     a.rpi_uri,
                "steer":       a.steer,
                "gain_taper":  a.gain_taper,
            }
            (sdir / "config.json").write_text(
                json.dumps(cfg, indent=2), encoding="utf-8")
            (sdir / "labels.json").write_text(
                json.dumps({"segments": []}, indent=2), encoding="utf-8")

            np.save(sdir / "cw_iq.npy",
                    np.stack(self._rec_iq, axis=0).astype(np.complex64))
            np.save(sdir / "timestamps.npy",
                    np.array(self._rec_ts, dtype=np.float64))
            (sdir / "vitals.json").write_text(
                json.dumps(self._rec_vitals, indent=2), encoding="utf-8")

            n = len(self._rec_iq)
            print(f"[mode_b] Saved {n} frames → {sdir}")
            self._save_lbl.setText(f"Saved {n} frames")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #88ff88;")
        except Exception as exc:
            print(f"[mode_b] Save failed: {exc}")
            self._save_lbl.setText("Save FAILED")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #ff4444;")

    def _on_quit(self) -> None:
        if self._saving:
            if hasattr(self, "_save_cb"):
                self._save_cb.setChecked(False)
            else:
                self._flush_recording()
        self.close()

    def closeEvent(self, event) -> None:
        if hasattr(self, "_worker"):
            self._worker.requestInterruption()
            self._worker.wait(3000)
        try:
            self._sdr.tx_destroy_buffer()
        except Exception:
            pass
        event.accept()


# ---------------------------------------------------------------------------
# Dark palette
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
# Hardware bring-up
# ---------------------------------------------------------------------------

def _bring_up(args: argparse.Namespace):
    """Connect to Pluto + Phaser and start cyclic CW TX.

    Matches radar_backends.py proven sequence exactly:
      - rx_enabled_channels = [0, 1]  (AD9361 needs both channels active)
      - tx_enabled_channels = [0, 1]  (TX0 muted at -88 dB, TX1 active)
      - sdr.tx([iq, iq]) with N = 2^18  (large cyclic buffer)
      - rx_buffer_size set AFTER sdr.tx()  (avoids ETIMEDOUT on first rx())

    Returns (sdr, phaser).
    """
    try:
        import adi
    except ImportError as exc:
        raise RuntimeError(
            "pyadi-iio is required. Activate the radar venv."
        ) from exc

    taper = tuple(int(v.strip()) for v in args.gain_taper.split(",") if v.strip())
    if len(taper) != 8:
        raise ValueError("--gain-taper must have 8 values")

    sdr    = adi.ad9361(uri=args.sdr_uri)
    sdr._ctx.set_timeout(5000)
    phaser = adi.CN0566(uri=args.rpi_uri, sdr=sdr)

    # Phaser RX array
    phaser.configure(device_mode="rx")
    try:
        phaser.element_spacing = 0.014
    except Exception:
        pass
    phaser.load_gain_cal()
    phaser.load_phase_cal()
    for ch in range(8):
        phaser.set_chan_phase(ch, 0)
    for ch, g in enumerate(taper):
        phaser.set_chan_gain(ch, g, apply_cal=True)

    def _gpio(name, val):
        for attr in ("_gpios", "gpios"):
            obj = getattr(phaser, attr, None)
            if obj is not None:
                setattr(obj, name, val)
                return

    _gpio("gpio_tx_sw", 0)     # TX_OUT_2
    _gpio("gpio_vctrl_1", 1)   # onboard PLL on
    _gpio("gpio_vctrl_2", 1)   # TX path enabled

    # SDR RX — both channels enabled (required by AD9361 firmware)
    sdr.sample_rate              = int(args.sample_rate)
    sdr.rx_lo                    = int(args.center_freq)
    sdr.rx_enabled_channels      = [0, 1]
    sdr.gain_control_mode_chan0   = "manual"
    sdr.gain_control_mode_chan1   = "manual"
    sdr.rx_hardwaregain_chan0     = int(args.rx_gain)
    sdr.rx_hardwaregain_chan1     = int(args.rx_gain)

    # SDR TX — both channels; TX0 muted, TX1 active
    sdr.tx_lo                    = int(args.center_freq)
    sdr.tx_enabled_channels      = [0, 1]
    sdr.tx_cyclic_buffer         = True
    sdr.tx_hardwaregain_chan0     = -88               # TX0 muted
    sdr.tx_hardwaregain_chan1     = int(args.tx_gain) # TX1 active

    # ADF4159 — CW (ramp disabled)
    vco_freq = int(args.output_freq + args.signal_freq + args.center_freq)
    phaser.frequency  = int(vco_freq / 4)
    phaser.ramp_mode  = "disabled"
    phaser.enable     = 0   # latch PLL registers
    _gpio("gpio_vctrl_1", 1)
    _gpio("gpio_vctrl_2", 1)

    # Build and load cyclic CW tone.
    # N = 2^18 — large independent cyclic buffer (matches radar_backends.py).
    # Do NOT use rx_buffer_size here; it must be set AFTER tx().
    fs  = int(args.sample_rate)
    N   = int(2 ** 18)
    fc  = args.signal_freq
    t   = np.arange(N) / fs
    i_  = np.cos(2.0 * np.pi * t * fc) * 2 ** 14
    q_  = np.sin(2.0 * np.pi * t * fc) * 2 ** 14
    iq  = (0.9 * (i_ + 1j * q_)).astype(np.complex64)
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass
    try:
        sdr.tx([iq, iq])   # two identical buffers — matches Mode A pattern
    except OSError as exc:
        if exc.errno == 16:
            raise RuntimeError(
                "TX buffer busy (errno=16). Unplug Pluto USB, wait 5 s, replug."
            ) from exc
        raise

    # rx_buffer_size MUST be set AFTER sdr.tx() — AD9361 firmware requirement
    sdr.rx_buffer_size = int(args.frame_size)

    print(
        f"[mode_b] RF={args.output_freq/1e9:.4f} GHz  "
        f"tone={args.signal_freq/1e3:.0f} kHz  "
        f"SR={args.sample_rate/1e3:.0f} kHz  "
        f"RX={args.rx_gain} dB  TX={args.tx_gain} dB"
    )

    if args.steer != 0.0:
        angle_rad   = np.radians(float(args.steer))
        phase_delta = (
            2.0 * np.pi * args.output_freq * 0.014
            * np.sin(angle_rad) / 3e8
        )
        phaser.set_beam_phase_diff(np.degrees(phase_delta))

    return sdr, phaser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    args   = build_parser().parse_args(argv)
    sdr, phaser = _bring_up(args)

    # Give the TX cyclic buffer and AD9361 LO time to settle before RX starts.
    # heartbeat1.m uses pause(0.1) for the same reason.
    time.sleep(0.3)

    app = QApplication(sys.argv)
    _dark_palette(app)

    win    = ModeBWindow(sdr, phaser, args)
    worker = CWWorker(sdr)
    win._worker = worker

    worker.frame_ready.connect(win.on_frame)
    worker.error.connect(
        lambda msg: print(f"[mode_b] CWWorker error: {msg}"))
    worker.start()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
