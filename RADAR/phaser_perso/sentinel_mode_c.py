"""SENTINEL Mode C — CW Reflectometry Muscle Proxy (EMG-proxy / myo-index).

Radiates a fixed CW tone at close range (<0.5 m) and tracks amplitude
modulations in the 10–500 Hz band caused by muscle contraction (water
redistribution / dielectric shift).

Architecture
------------
  myo_dsp.py           MyoProcessor — mix, amplitude envelope, decimate,
                        spectral energy + centroid, myo-index normalisation
  sentinel_mode_c.py   hardware bring-up + QThread worker + PyQt5/pyqtgraph GUI

Hardware topology (Windows, proven)
------------------------------------
  PC → Pluto  via  ip:192.168.2.1      RX0+RX1 both enabled (AD9361 requirement)
  PC → Phaser via  ip:phaser.local     ADF4159 CW (ramp disabled)

  TX1: cyclic 100 kHz tone, N=2^18 buffer, TX0 muted at -88 dB.
  rx_buffer_size set AFTER sdr.tx([iq,iq]).
  Do NOT use ip:phaser.local:50901 — ETIMEDOUT on Windows.

DSP
---
  IF IQ @ 600 kHz  →  mix to baseband  →  abs(envelope)
  →  decimate ÷ 300  →  ~2 kHz stream  →  FFT of 0.25 s window
  →  spectral energy & centroid in [10–500 Hz]
  →  myo-index normalised to calibration baseline  →  activation flag

Interpretation
--------------
  Mode C is not contact EMG. It is a radar reflectometry proxy for muscle
  activity: contraction can modulate reflected amplitude through small
  motion, water redistribution, and dielectric changes in tissue. Treat
  myo-index as a relative activation/trend feature. Confirm it with controlled
  clench/rest trials and, when possible, synchronized contact EMG or video.

GUI panels
----------
  Top-left    : IF Spectrum  — zoomed ±2 kHz around 100 kHz tone
  Top-right   : Amplitude spectrum  — 0–600 Hz (current window PSD)
  Bottom-left : Myo-index trace  — 10 s rolling normalised energy
  Bottom-right: Centroid trace  — 10 s rolling spectral centroid (Hz)

Run
---
  python sentinel_mode_c.py
  python sentinel_mode_c.py --rx-gain 55 --threshold 0.25
  python sentinel_mode_c.py --no-record
"""
from __future__ import annotations

import argparse
import collections
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

from myo_dsp import MyoProcessor, MyoResult


# ---------------------------------------------------------------------------
# pyqtgraph global config
# ---------------------------------------------------------------------------
pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
pg.setConfigOption("background", "#1a1a2e")
pg.setConfigOption("foreground", "#dddddd")

LABEL_KW = {"color": "#ccc", "font-size": "10pt"}

CHEBYSHEV_TAPER = (8, 34, 84, 127, 127, 84, 34, 8)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel_mode_c",
        description="SENTINEL Mode C: CW reflectometry muscle proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    hw = p.add_argument_group("hardware URIs")
    hw.add_argument("--rpi-uri", default="ip:phaser.local")
    hw.add_argument("--sdr-uri", default="ip:192.168.2.1",
                    help="Pluto IIO URI. Do NOT use ip:phaser.local:50901.")

    r = p.add_argument_group("RF / CW tuning")
    r.add_argument("--output-freq", type=float, default=10.25e9)
    r.add_argument("--center-freq", type=float, default=2.1e9)
    r.add_argument("--signal-freq", type=float, default=100e3)
    r.add_argument("--sample-rate", type=float, default=600e3)
    r.add_argument("--frame-size", type=int, default=8192)
    r.add_argument("--rx-gain", type=int, default=50,
                   help="RX0/RX1 hardware gain (dB)")
    r.add_argument("--tx-gain", type=int, default=-6,
                   help="TX1 hardware gain (dB)")
    r.add_argument("--gain-taper",
                   default=",".join(str(g) for g in CHEBYSHEV_TAPER),
                   metavar="g0,..,g7")
    r.add_argument("--steer", type=float, default=0.0, metavar="DEG")

    d = p.add_argument_group("DSP / display")
    d.add_argument("--phase-fs", type=float, default=2000.0,
                   help="Envelope sample rate after decimation (Hz)")
    d.add_argument("--history-sec", type=float, default=10.0,
                   help="Myo trace rolling window (s)")
    d.add_argument("--baseline-sec", type=float, default=3.0,
                   help="Calibration baseline duration (s)")
    d.add_argument("--win-sec", type=float, default=0.25,
                   help="Spectral analysis window (s)")
    d.add_argument("--band-lo", type=float, default=10.0,
                   help="EMG proxy band low cutoff (Hz)")
    d.add_argument("--band-hi", type=float, default=500.0,
                   help="EMG proxy band high cutoff (Hz)")
    d.add_argument("--threshold", type=float, default=0.3,
                   help="Myo-index activation threshold (0–1)")

    s = p.add_argument_group("recording")
    s.add_argument("--save-dir", type=Path, default=Path("recordings"))
    s.add_argument("--no-record", action="store_true")
    return p


# ---------------------------------------------------------------------------
# Acquisition worker
# ---------------------------------------------------------------------------

class CWWorker(QThread):
    frame_ready = pyqtSignal(object)
    error       = pyqtSignal(str)

    def __init__(self, sdr) -> None:
        super().__init__()
        self._sdr = sdr

    def run(self) -> None:
        consec_errors = 0
        while not self.isInterruptionRequested():
            try:
                raw = self._sdr.rx()
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
                    break
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ModeCWindow(QMainWindow):

    _WF_SPAN_HZ = 2000.0
    _IF_DB_MIN  = -110.0
    _IF_DB_MAX  = 0.0

    def __init__(self, sdr, phaser, args: argparse.Namespace) -> None:
        super().__init__()
        self._sdr    = sdr
        self._phaser = phaser
        self._args   = args

        self._proc = MyoProcessor(
            fs           = args.sample_rate,
            signal_freq  = args.signal_freq,
            phase_fs     = args.phase_fs,
            history_sec  = args.history_sec,
            baseline_sec = args.baseline_sec,
            win_sec      = args.win_sec,
            band_lo      = args.band_lo,
            band_hi      = args.band_hi,
            threshold    = args.threshold,
        )

        # IF FFT axis (raw ADC rate)
        n = args.frame_size
        self._freq_axis = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / args.sample_rate))
        self._win_fft   = np.blackman(n).astype(np.float32)
        self._win_scale = float(np.sum(self._win_fft))

        # Rolling traces (GUI maintains them; MyoResult is a per-call scalar)
        trace_len = max(100, round(args.history_sec * 10))  # ~10 updates/s
        self._myo_trace      = collections.deque([0.0] * trace_len, maxlen=trace_len)
        self._centroid_trace = collections.deque([args.band_lo] * trace_len, maxlen=trace_len)
        self._trace_t        = np.linspace(-args.history_sec, 0.0, trace_len, dtype=np.float32)

        # Recording state
        self._saving   = False
        self._rec_iq:  list[np.ndarray] = []
        self._rec_ts:  list[float]      = []
        self._rec_myo: list[dict]       = []
        self._frame_count = 0

        self.setWindowTitle(
            f"SENTINEL Mode C  [Myo]  "
            f"RF={args.output_freq/1e9:.3f} GHz  "
            f"SR={args.sample_rate/1e3:.0f} kHz  "
            f"env_fs={self._proc.phase_fs:.0f} Hz"
        )
        self._build_ui()
        self.showMaximized()

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
        box = QGroupBox("Mode C Controls")
        box.setFixedWidth(220)
        lay = QVBoxLayout(box)
        lay.setSpacing(4)

        def lbl(text, small=False):
            w = QLabel(text)
            w.setStyleSheet(f"font-size: {'10' if small else '11'}px; color: #ccc;")
            return w

        def slider(lo, hi, val, tick=0):
            s = QSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(val)
            if tick:
                s.setTickInterval(tick)
                s.setTickPosition(QSlider.TicksBelow)
            return s

        # Activation indicator
        self._act_lbl = QLabel("  rest  ")
        self._act_lbl.setAlignment(Qt.AlignCenter)
        self._act_lbl.setStyleSheet(
            "font-size: 20px; font-weight: bold; "
            "background-color: #333355; color: #888888; "
            "border-radius: 6px; padding: 6px;"
        )
        lay.addWidget(self._act_lbl)

        # Myo-index bar
        self._myo_bar_lbl = lbl("index: –", True)
        lay.addWidget(self._myo_bar_lbl)

        # Calibration status
        self._cal_lbl = lbl("Calibrating…", True)
        self._cal_lbl.setStyleSheet("font-size: 10px; color: #ffaa44;")
        lay.addWidget(self._cal_lbl)

        # Recalibrate button
        recal_btn = QPushButton("Recalibrate")
        recal_btn.setFixedHeight(24)
        recal_btn.clicked.connect(self._on_recalibrate)
        lay.addWidget(recal_btn)

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
        apply_btn = QPushButton("Apply RX Gain")
        apply_btn.setFixedHeight(24)
        apply_btn.clicked.connect(self._on_rx_gain_apply)
        lay.addWidget(self._rxgain_sl)
        lay.addWidget(self._rxgain_lbl)
        lay.addWidget(apply_btn)

        # Activation threshold
        lay.addWidget(lbl("Threshold (×10)"))
        thr_init = int(self._args.threshold * 10)
        self._thr_sl  = slider(1, 10, thr_init, 1)
        self._thr_lbl = lbl(f"{self._args.threshold:.1f}", True)
        def _thr_changed(v):
            t = v / 10.0
            self._thr_lbl.setText(f"{t:.1f}")
            self._proc.threshold = t
        self._thr_sl.valueChanged.connect(_thr_changed)
        lay.addWidget(self._thr_sl)
        lay.addWidget(self._thr_lbl)

        # Recording
        if not self._args.no_record:
            self._save_cb = QCheckBox("Record session")
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
        sig  = self._args.signal_freq
        span = self._WF_SPAN_HZ

        # ── IF Spectrum (top-left) ─────────────────────────────────────
        p_if = glw.addPlot(row=0, col=0, title="IF Spectrum  (dBFS)")
        p_if.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_if.setLabel("left",   "dBFS",           **LABEL_KW)
        p_if.setXRange(sig - span, sig + span, padding=0)
        p_if.setYRange(self._IF_DB_MIN, self._IF_DB_MAX, padding=0)
        p_if.enableAutoRange("xy", False)
        p_if.showGrid(x=True, y=True, alpha=0.2)
        p_if.addItem(pg.InfiniteLine(
            pos=sig, angle=90, movable=False,
            pen=pg.mkPen(color=(80, 255, 120), width=1, style=Qt.DashLine),
        ))
        self._if_curve = p_if.plot(pen=pg.mkPen(color=(255, 200, 50), width=1.5))

        # ── Amplitude PSD 0–600 Hz (top-right) ────────────────────────
        p_amp = glw.addPlot(row=0, col=1, title="Amplitude Spectrum  (EMG band)")
        p_amp.setLabel("bottom", "Frequency [Hz]", **LABEL_KW)
        p_amp.setLabel("left",   "PSD [dB]",       **LABEL_KW)
        p_amp.setXRange(0.0, 600.0, padding=0)
        p_amp.enableAutoRange("y", True)
        p_amp.enableAutoRange("x", False)
        p_amp.showGrid(x=True, y=True, alpha=0.2)
        # EMG band shading
        p_amp.addItem(pg.LinearRegionItem(
            [self._args.band_lo, self._args.band_hi], movable=False,
            brush=pg.mkBrush(255, 165, 0, 30),
            pen=pg.mkPen(color=(255, 165, 0), width=0.5),
        ))
        self._amp_curve = p_amp.plot(pen=pg.mkPen(color=(255, 165, 0), width=1.5))

        # ── Myo-index trace (bottom-left) ─────────────────────────────
        p_myo = glw.addPlot(row=1, col=0, title="Myo-index  (normalised)")
        p_myo.setLabel("bottom", "Time [s]",  **LABEL_KW)
        p_myo.setLabel("left",   "Myo-index", **LABEL_KW)
        p_myo.setXRange(-self._args.history_sec, 0.0, padding=0)
        p_myo.setYRange(0.0, 1.0, padding=0)
        p_myo.enableAutoRange("xy", False)
        p_myo.showGrid(x=True, y=True, alpha=0.2)
        # Threshold line
        self._thr_line = pg.InfiniteLine(
            pos=self._args.threshold, angle=0, movable=False,
            pen=pg.mkPen(color=(255, 80, 80), width=1, style=Qt.DashLine),
        )
        p_myo.addItem(self._thr_line)
        self._myo_curve = p_myo.plot(
            pen=pg.mkPen(color=(255, 165, 0), width=2))

        # ── Centroid trace (bottom-right) ──────────────────────────────
        p_cen = glw.addPlot(row=1, col=1, title="Spectral Centroid  (Hz)")
        p_cen.setLabel("bottom", "Time [s]",   **LABEL_KW)
        p_cen.setLabel("left",   "Centroid [Hz]", **LABEL_KW)
        p_cen.setXRange(-self._args.history_sec, 0.0, padding=0)
        p_cen.setYRange(self._args.band_lo, self._args.band_hi, padding=0.05)
        p_cen.enableAutoRange("y", True)
        p_cen.enableAutoRange("x", False)
        p_cen.showGrid(x=True, y=True, alpha=0.2)
        self._cen_curve = p_cen.plot(
            pen=pg.mkPen(color=(100, 200, 255), width=2))

        glw.ci.layout.setRowStretchFactor(0, 2)
        glw.ci.layout.setRowStretchFactor(1, 3)

        return glw

    # ------------------------------------------------------------------

    def on_frame(self, raw_iq: np.ndarray) -> None:
        t0 = time.monotonic()
        self._frame_count += 1
        n = len(raw_iq)

        # 1. IF FFT for display
        data = raw_iq[:len(self._win_fft)] if n >= len(self._win_fft) else (
            np.pad(raw_iq, (0, len(self._win_fft) - n)))
        sp   = np.fft.fftshift(np.fft.fft(data * self._win_fft))
        s_db = (20.0 * np.log10(
            np.maximum(np.abs(sp) / self._win_scale / 2.0**11, 1e-15)
        )).astype(np.float32)
        self._if_curve.setData(self._freq_axis, s_db)

        # 2. Push into MyoProcessor
        self._proc.push(raw_iq)

        # 3. Compute myo-features every 5 frames (~8 ms * 5 = 40 ms → 25 Hz)
        if self._frame_count % 5 == 0:
            result = self._proc.compute()
            if result is not None:
                self._update_myo_panels(result)

        # 4. Recording
        if self._saving:
            self._rec_iq.append(raw_iq.copy())
            self._rec_ts.append(t0)

        dt_ms = (time.monotonic() - t0) * 1000
        self._status_lbl.setText(
            f"frame: {self._frame_count}  proc: {dt_ms:.0f} ms\n"
            f"saved: {len(self._rec_iq)}"
        )

    def _update_myo_panels(self, r: MyoResult) -> None:
        # Amplitude spectrum
        if len(r.spec_freq_hz) > 1:
            self._amp_curve.setData(x=r.spec_freq_hz, y=r.spec_db)

        # Rolling traces
        self._myo_trace.append(r.myo_index)
        self._centroid_trace.append(r.cur_centroid_hz)
        myo_arr = np.array(self._myo_trace, dtype=np.float32)
        cen_arr = np.array(self._centroid_trace, dtype=np.float32)
        self._myo_curve.setData(x=self._trace_t, y=myo_arr)
        self._cen_curve.setData(x=self._trace_t, y=cen_arr)

        # Threshold line
        self._thr_line.setValue(self._proc.threshold)

        # Activation indicator
        if r.calibrated:
            if r.activation:
                self._act_lbl.setText("  ACTIVE  ")
                self._act_lbl.setStyleSheet(
                    "font-size: 20px; font-weight: bold; "
                    "background-color: #882222; color: #ffcccc; "
                    "border-radius: 6px; padding: 6px;"
                )
            else:
                self._act_lbl.setText("  rest  ")
                self._act_lbl.setStyleSheet(
                    "font-size: 20px; font-weight: bold; "
                    "background-color: #224422; color: #aaffaa; "
                    "border-radius: 6px; padding: 6px;"
                )
            self._cal_lbl.setText(
                f"Calibrated  E₀={r.cur_energy_db:.1f} dB")
            self._cal_lbl.setStyleSheet("font-size: 10px; color: #88cc88;")
        else:
            pct = int(r.cal_progress * 100)
            self._cal_lbl.setText(f"Calibrating… {pct}%")
            self._cal_lbl.setStyleSheet("font-size: 10px; color: #ffaa44;")
            self._act_lbl.setText("  cal…  ")
            self._act_lbl.setStyleSheet(
                "font-size: 20px; font-weight: bold; "
                "background-color: #333355; color: #888888; "
                "border-radius: 6px; padding: 6px;"
            )

        bar_n = int(r.myo_index * 10)
        self._myo_bar_lbl.setText(
            f"index: {'▮'*bar_n}{'▯'*(10-bar_n)}  {r.myo_index:.2f}  "
            f"centroid: {r.cur_centroid_hz:.0f} Hz"
        )

        # Save myo snapshot ~1/s
        if self._saving and self._frame_count % 70 == 0:
            self._rec_myo.append({
                "t":          float(time.monotonic()),
                "myo_index":  r.myo_index,
                "activation": r.activation,
                "energy_db":  r.cur_energy_db,
                "centroid_hz": r.cur_centroid_hz,
            })

    # ------------------------------------------------------------------

    def _on_recalibrate(self) -> None:
        self._proc.reset_calibration()
        self._cal_lbl.setText("Calibrating…")
        self._cal_lbl.setStyleSheet("font-size: 10px; color: #ffaa44;")
        print("[mode_c] Recalibrating baseline…")

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
            self._rec_myo.clear()
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
            sdir = base / datetime.now().strftime("%Y%m%d_%H%M%S_modeC")
            sdir.mkdir()

            a = self._args
            cfg = {
                "mode":        "C_myo",
                "output_freq": a.output_freq,
                "center_freq": a.center_freq,
                "signal_freq": a.signal_freq,
                "sample_rate": a.sample_rate,
                "frame_size":  a.frame_size,
                "rx_gain":     a.rx_gain,
                "tx_gain":     a.tx_gain,
                "phase_fs":    self._proc.phase_fs,
                "band_lo":     a.band_lo,
                "band_hi":     a.band_hi,
                "threshold":   self._proc.threshold,
            }
            (sdir / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            (sdir / "labels.json").write_text(
                json.dumps({"segments": []}, indent=2), encoding="utf-8")
            np.save(sdir / "cw_iq.npy",
                    np.stack(self._rec_iq, axis=0).astype(np.complex64))
            np.save(sdir / "timestamps.npy",
                    np.array(self._rec_ts, dtype=np.float64))
            (sdir / "myo.json").write_text(
                json.dumps(self._rec_myo, indent=2), encoding="utf-8")

            n = len(self._rec_iq)
            print(f"[mode_c] Saved {n} frames → {sdir}")
            self._save_lbl.setText(f"Saved {n} frames")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #88ff88;")
        except Exception as exc:
            print(f"[mode_c] Save failed: {exc}")
            self._save_lbl.setText("Save FAILED")
            self._save_lbl.setStyleSheet("font-size: 10px; color: #ff4444;")

    def _on_quit(self) -> None:
        if self._saving and hasattr(self, "_save_cb"):
            self._save_cb.setChecked(False)
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
# Hardware bring-up  (identical proven pattern as sentinel_mode_b.py)
# ---------------------------------------------------------------------------

def _bring_up(args: argparse.Namespace):
    """Connect to Pluto + Phaser and start cyclic CW TX.

    Identical bring-up sequence to sentinel_mode_b.py:
      rx_enabled_channels = [0, 1], tx_enabled_channels = [0, 1],
      TX0 muted at -88 dB, sdr.tx([iq,iq]) with N=2^18,
      rx_buffer_size set AFTER sdr.tx().
    """
    try:
        import adi
    except ImportError as exc:
        raise RuntimeError("pyadi-iio is required. Activate the radar venv.") from exc

    taper = tuple(int(v.strip()) for v in args.gain_taper.split(",") if v.strip())
    if len(taper) != 8:
        raise ValueError("--gain-taper must have 8 values")

    sdr    = adi.ad9361(uri=args.sdr_uri)
    sdr._ctx.set_timeout(5000)
    phaser = adi.CN0566(uri=args.rpi_uri, sdr=sdr)

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

    _gpio("gpio_tx_sw", 0)
    _gpio("gpio_vctrl_1", 1)
    _gpio("gpio_vctrl_2", 1)

    # SDR RX — both channels
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
    sdr.tx_hardwaregain_chan0     = -88
    sdr.tx_hardwaregain_chan1     = int(args.tx_gain)

    # ADF4159 — CW
    vco_freq = int(args.output_freq + args.signal_freq + args.center_freq)
    phaser.frequency = int(vco_freq / 4)
    phaser.ramp_mode = "disabled"
    phaser.enable    = 0
    _gpio("gpio_vctrl_1", 1)
    _gpio("gpio_vctrl_2", 1)

    # Cyclic TX tone — N=2^18, independent of rx_buffer_size
    fs  = int(args.sample_rate)
    N   = int(2 ** 18)
    fc  = args.signal_freq
    t   = np.arange(N) / fs
    iq  = (0.9 * (np.cos(2.0 * np.pi * t * fc) +
                  1j * np.sin(2.0 * np.pi * t * fc)) * 2**14).astype(np.complex64)
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass
    try:
        sdr.tx([iq, iq])
    except OSError as exc:
        if exc.errno == 16:
            raise RuntimeError(
                "TX buffer busy (errno=16). Unplug Pluto USB, wait 5 s, replug."
            ) from exc
        raise

    # rx_buffer_size AFTER tx()
    sdr.rx_buffer_size = int(args.frame_size)

    print(
        f"[mode_c] RF={args.output_freq/1e9:.4f} GHz  "
        f"tone={args.signal_freq/1e3:.0f} kHz  "
        f"SR={args.sample_rate/1e3:.0f} kHz  "
        f"RX={args.rx_gain} dB  TX={args.tx_gain} dB  "
        f"env_fs={int(args.sample_rate) // max(1, round(args.sample_rate / args.phase_fs))} Hz"
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
    args = build_parser().parse_args(argv)
    sdr, phaser = _bring_up(args)
    time.sleep(0.3)   # let TX settle before first rx()

    app = QApplication(sys.argv)
    _dark_palette(app)

    win    = ModeCWindow(sdr, phaser, args)
    worker = CWWorker(sdr)
    win._worker = worker

    worker.frame_ready.connect(win.on_frame)
    worker.error.connect(
        lambda msg: print(f"[mode_c] CWWorker error: {msg}"))
    worker.start()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
