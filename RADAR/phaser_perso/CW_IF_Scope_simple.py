#!/usr/bin/env python3
# Purpose: CW radar IF scope for CN0566/Pluto. Shows FFT, waterfall, and time‑domain IQ.
# Based on: workspace/radar/pyadi-iio/examples/phaser/PhaserRadarLabs/CW_RADAR_Waterfall.py
#
# NOTE ON FREQUENCY & SPECTRUM ANALYZER READINGS:
# This system uses "High-Side Mixing": RF_OUT = LO_FREQ - IF_FREQ.
# - IF_FREQ is fixed at 2.2 GHz (Pluto).
# - If you set RF_DEFAULT = 10.2 GHz, the script sets LO = 12.4 GHz.
# - Hardware isolation is finite (~30dB). On a spectrum analyzer, you will see:
#     1. The desired signal at 10.2 GHz.
#     2. The LO leakage at 12.4 GHz (often strong).
# This is normal hardware behavior, not a software bug.
#
# NOTE ON VIBRATION DETECTION (Micro-Doppler):
# A vibrating target (like a speaker at 157 Hz) does NOT produce a single shifted peak.
# It produces FM Sidebands (Harmonics) at Carrier +/- 157 Hz, +/- 314 Hz, etc.
# Look for these symmetric sidebands in the "Audio/Doppler FFT" plot.

import sys
from datetime import datetime

import adi
import numpy as np
import warnings
import pyqtgraph as pg
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QLabel,
    QGridLayout,
    QPushButton,
    QSlider,
    QWidget,
    QMainWindow,
    QDoubleSpinBox,
)
from pyqtgraph.Qt import QtCore, QtGui


# --------------------------------------------------------------------------- #
# User-tunable parameters (edit here; everything else derives from these)
# --------------------------------------------------------------------------- #
RPI_IP = "ip:phaser.local"
SDR_IP = "ip:phaser.local:50901"
SAMPLE_RATE = 0.6e6          # Pluto ADC/DAC rate (Hz) - AD9361 min practical ~521 kHz
IF_FREQ = 2.2e9              # Pluto LO / IF (Hz)
SIGNAL_TONE = 100e3          # Baseband tone sent by Pluto (Hz)
RF_MIN = 10.0e9              # Allowed RF range (Hz)
RF_MAX = 10.5e9
RF_DEFAULT = 10.045e9        # Desired radiated RF (Hz)
NUM_SLICES = 50              # Waterfall depth
FFT_SIZE = 8192              # Smaller buffer reduces lag; still <75 Hz/bin @0.6 MS/s
RX_GAIN_CH0 = 50             # dB, manual gain
RX_GAIN_CH1 = 50
TX_GAIN_CH1 = -6             # Leave some headroom on TX PA
TX_SCALE_CH1 = 1.0           # Relative amplitude for I data fed to TX1
GAIN_TAPER = [8, 34, 84, 127, 127, 84, 34, 8]  # Blackman taper for the 8 RX elems
BB_DECIM = 64                # Decimation for baseband/time view (longer window, finer Hz resolution)
FFT_SPAN_HZ = 50000          # Wider default span around tone (±50 kHz)
WF_SPAN_HZ = 50000           # Match waterfall span
WATER_LOW_DB = -100          # Initial waterfall lower level
WATER_HIGH_DB = -30          # Initial waterfall upper level
AUDIO_HP_SMOOTH_SEC = 0.04   # Placeholder (kept for compatibility; not used)
AUDIO_FFT_LEN = 16384        # Length for low-frequency (heartbeat) FFT
TIME_HISTORY_SEC = 8.0       # Length of baseband history shown (s)
fft_span_hz_default = FFT_SPAN_HZ
wf_span_hz_default = WF_SPAN_HZ
bb_decim_default = BB_DECIM
time_history_sec_default = TIME_HISTORY_SEC

# Mutable runtime copies (updated via GUI)
rf_freq = RF_DEFAULT
signal_freq = SIGNAL_TONE
sample_rate = SAMPLE_RATE
center_freq = IF_FREQ
lo_freq = rf_freq + center_freq           # High-side LO = RF + IF
num_slices = NUM_SLICES
fft_size = FFT_SIZE
rx_gain_ch0 = RX_GAIN_CH0
rx_gain_ch1 = RX_GAIN_CH1
tx_gain_ch1 = TX_GAIN_CH1
tx_scale_ch1 = TX_SCALE_CH1
GAIN_TAPER_RUNTIME = GAIN_TAPER.copy()
bb_decim = bb_decim_default
audio_hp_smooth_sec = AUDIO_HP_SMOOTH_SEC
fft_span_hz = fft_span_hz_default
wf_span_hz = wf_span_hz_default
time_history_sec = time_history_sec_default


# --------------------------------------------------------------------------- #
# Derived parameters (do not edit below unless changing algorithm)
# --------------------------------------------------------------------------- #
signal_freq = SIGNAL_TONE
rf_freq = RF_DEFAULT
lo_freq = rf_freq + IF_FREQ           # High-side LO = RF + IF
sample_rate = SAMPLE_RATE
center_freq = IF_FREQ
num_slices = NUM_SLICES
fft_size = FFT_SIZE

# --------------------------------------------------------------------------- #
# Precompute optimization globals
# --------------------------------------------------------------------------- #
mix_exp = np.exp(-1j * 2 * np.pi * signal_freq * np.arange(fft_size) / sample_rate).astype(np.complex64)
win_funct = np.blackman(fft_size)

# Instantiate hardware
my_sdr = adi.ad9361(uri=SDR_IP)
my_phaser = adi.CN0566(uri=RPI_IP, sdr=my_sdr)


def _route_tx_out2_and_lo():
    """Ensure LO source and TX routing stay asserted (Phaser GPIOs)."""
    try:
        my_phaser._gpios.gpio_tx_sw = 0  # TX_OUT_2
        my_phaser._gpios.gpio_vctrl_1 = 1  # Internal PLL/VCO on
        my_phaser._gpios.gpio_vctrl_2 = 1  # Route LO to TX path
    except AttributeError:
        my_phaser.gpios.gpio_tx_sw = 0
        my_phaser.gpios.gpio_vctrl_1 = 1
        my_phaser.gpios.gpio_vctrl_2 = 1


def _load_tx_tone(iq_vec):
    """(Re)load the cyclic TX buffer so CW stays on after Apply."""
    my_sdr.tx_destroy_buffer()
    my_sdr.tx_cyclic_buffer = True
    my_sdr.tx(iq_vec)  # TX1 only (configured below)

# Phaser bring-up
my_phaser.configure(device_mode="rx")
my_phaser.load_gain_cal()
my_phaser.load_phase_cal()
for i in range(8):
    my_phaser.set_chan_phase(i, 0)
for i, g in enumerate(GAIN_TAPER):
    my_phaser.set_chan_gain(i, g, apply_cal=True)
_route_tx_out2_and_lo()

# SDR configuration
my_sdr.sample_rate = int(sample_rate)
my_sdr.rx_lo = int(center_freq)
my_sdr.rx_enabled_channels = [0]  # use RX0 only for higher SNR (avoid summing mismatch)
my_sdr.rx_buffer_size = int(fft_size)
my_sdr.gain_control_mode_chan0 = "manual"
my_sdr.gain_control_mode_chan1 = "manual"
my_sdr.rx_hardwaregain_chan0 = rx_gain_ch0
my_sdr.rx_hardwaregain_chan1 = rx_gain_ch1
my_sdr.tx_lo = int(center_freq)
my_sdr.tx_enabled_channels = [1]  # drive TX1 only
my_sdr.tx_cyclic_buffer = True
my_sdr.tx_hardwaregain_chan1 = tx_gain_ch1

# ADF4159 (driver expects LO/4 at .frequency)
my_phaser.frequency = int(lo_freq / 4)
my_phaser.ramp_mode = "disabled"
my_phaser.enable = 0

# Build transmit IQ
fs = int(my_sdr.sample_rate)
N = int(my_sdr.rx_buffer_size)
fc = int(signal_freq / (fs / N)) * (fs / N)
ts = 1 / float(fs)
t = np.arange(0, N * ts, ts)
i = np.cos(2 * np.pi * t * fc) * 2**14
q = np.sin(2 * np.pi * t * fc) * 2**14
iq = (i + 1j * q).astype(np.complex64)
my_sdr._ctx.set_timeout(0)
_load_tx_tone(iq * tx_scale_ch1)  # keep CW alive

freq = np.linspace(-fs / 2, fs / 2, int(N))
time_axis = np.arange(N) / fs
# baseband decimation for time-domain view (higher decim = longer window, lower rate)
bb_decim = BB_DECIM  # 600 kHz / 16 ≈ 37.5 kS/s view rate by default
view_fs = fs / bb_decim
history_len = int(time_history_sec * view_fs)
time_axis_bb = np.arange(history_len) / view_fs
bb_hist = np.zeros(history_len, dtype=complex)

# Silence numpy complex casting warnings that clutter the console
warnings.filterwarnings("ignore", category=np.ComplexWarning)

# Qt application
App = QApplication(sys.argv)
UPDATE_PERIOD_S = 0.05  # 50 ms cadence


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CW IF Scope")
        self.setGeometry(50, 50, 1600, 1000)
        self.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, False)
        self.img_array = np.ones((num_slices, fft_size)) * (-100)
        self._latest = None  # last complex buffer
        self._build_ui()
        self.showMaximized()

    def _build_ui(self):
        widget = QWidget()
        layout = QGridLayout()

        title = QLabel("PHASER CW IF Scope")
        f = title.font()
        f.setPointSize(22)
        title.setFont(f)
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title, 0, 0, 1, 2)

        # Control panel: signal freq, audio span, RF out
        ctrl_font = title.font()
        ctrl_font.setPointSize(10)

        self.sig_label = QLabel(f"Signal freq (kHz): {signal_freq/1e3:0.1f}")
        self.sig_label.setFont(ctrl_font)
        layout.addWidget(self.sig_label, 1, 0, 1, 1)
        self.sig_spin = QDoubleSpinBox()
        self.sig_spin.setRange(10, 250)
        self.sig_spin.setSingleStep(1)
        self.sig_spin.setValue(signal_freq / 1e3)
        layout.addWidget(self.sig_spin, 1, 1, 1, 1)

        # Spacer row
        layout.addWidget(QLabel(""), 2, 0, 1, 2)

        self.lo_label = QLabel(f"RF out (GHz): {rf_freq/1e9:0.4f}  |  LO (GHz): {lo_freq/1e9:0.4f}")
        self.lo_label.setFont(ctrl_font)
        layout.addWidget(self.lo_label, 3, 0, 1, 2)
        self.lo_spin = QDoubleSpinBox()
        self.lo_spin.setRange(RF_MIN/1e9, RF_MAX/1e9)
        self.lo_spin.setDecimals(4)
        self.lo_spin.setSingleStep(0.01)
        self.lo_spin.setValue(rf_freq/1e9)
        layout.addWidget(self.lo_spin, 4, 0, 1, 2)

        self.apply_button = QPushButton("Apply settings")
        self.apply_button.pressed.connect(self.apply_settings)
        layout.addWidget(self.apply_button, 5, 0, 1, 2)

        # RX/TX gains
        self.rxg0_label = QLabel(f"RX0 gain (dB): {rx_gain_ch0}")
        self.rxg0_label.setFont(ctrl_font)
        layout.addWidget(self.rxg0_label, 6, 0)
        self.rxg0_spin = QDoubleSpinBox()
        self.rxg0_spin.setRange(-3, 70)
        self.rxg0_spin.setSingleStep(1)
        self.rxg0_spin.setValue(rx_gain_ch0)
        layout.addWidget(self.rxg0_spin, 6, 1)

        self.rxg1_label = QLabel(f"RX1 gain (dB): {rx_gain_ch1}")
        self.rxg1_label.setFont(ctrl_font)
        layout.addWidget(self.rxg1_label, 7, 0)
        self.rxg1_spin = QDoubleSpinBox()
        self.rxg1_spin.setRange(-3, 70)
        self.rxg1_spin.setSingleStep(1)
        self.rxg1_spin.setValue(rx_gain_ch1)
        layout.addWidget(self.rxg1_spin, 7, 1)

        self.txg1_label = QLabel(f"TX gain (dB): {tx_gain_ch1}")
        self.txg1_label.setFont(ctrl_font)
        layout.addWidget(self.txg1_label, 8, 0)
        self.txg1_spin = QDoubleSpinBox()
        self.txg1_spin.setRange(-88, 0)
        self.txg1_spin.setSingleStep(1)
        self.txg1_spin.setValue(tx_gain_ch1)
        layout.addWidget(self.txg1_spin, 8, 1)

        self.txscale1_label = QLabel(f"TX scale: {tx_scale_ch1:0.2f}")
        self.txscale1_label.setFont(ctrl_font)
        layout.addWidget(self.txscale1_label, 9, 0)
        self.txscale1_spin = QDoubleSpinBox()
        self.txscale1_spin.setRange(0.0, 1.0)
        self.txscale1_spin.setSingleStep(0.05)
        self.txscale1_spin.setValue(tx_scale_ch1)
        layout.addWidget(self.txscale1_spin, 9, 1)

        # Sample rate and FFT size
        self.sr_label = QLabel(f"Sample rate (MS/s): {sample_rate/1e6:0.3f}")
        self.sr_label.setFont(ctrl_font)
        layout.addWidget(self.sr_label, 12, 0)
        self.sr_spin = QDoubleSpinBox()
        self.sr_spin.setRange(0.521, 3.0)
        self.sr_spin.setDecimals(3)
        self.sr_spin.setSingleStep(0.05)
        self.sr_spin.setValue(sample_rate / 1e6)
        layout.addWidget(self.sr_spin, 12, 1)

        self.fftsize_label = QLabel(f"FFT size (samples): {fft_size}")
        self.fftsize_label.setFont(ctrl_font)
        layout.addWidget(self.fftsize_label, 13, 0)
        self.fftsize_spin = QDoubleSpinBox()
        self.fftsize_spin.setRange(1024, 65536)
        self.fftsize_spin.setDecimals(0)
        self.fftsize_spin.setSingleStep(1024)
        self.fftsize_spin.setValue(fft_size)
        layout.addWidget(self.fftsize_spin, 13, 1)

        # Decimation and spans
        self.decim_label = QLabel(f"BB decim: {bb_decim}")
        self.decim_label.setFont(ctrl_font)
        layout.addWidget(self.decim_label, 14, 0)
        self.decim_spin = QDoubleSpinBox()
        self.decim_spin.setRange(1, 128)
        self.decim_spin.setDecimals(0)
        self.decim_spin.setSingleStep(1)
        self.decim_spin.setValue(bb_decim)
        layout.addWidget(self.decim_spin, 14, 1)

        self.timehist_label = QLabel(f"Time window (s): {time_history_sec:0.2f}")
        self.timehist_label.setFont(ctrl_font)
        layout.addWidget(self.timehist_label, 15, 0)
        self.timehist_spin = QDoubleSpinBox()
        self.timehist_spin.setRange(0.05, 10.0)
        self.timehist_spin.setSingleStep(0.05)
        self.timehist_spin.setValue(time_history_sec)
        layout.addWidget(self.timehist_spin, 15, 1)

        self.fftspan_label = QLabel(f"FFT span (+/- Hz): {fft_span_hz}")
        self.fftspan_label.setFont(ctrl_font)
        layout.addWidget(self.fftspan_label, 16, 0)
        self.fftspan_spin = QDoubleSpinBox()
        self.fftspan_spin.setRange(50, sample_rate / 2)
        self.fftspan_spin.setSingleStep(50)
        self.fftspan_spin.setValue(fft_span_hz)
        layout.addWidget(self.fftspan_spin, 16, 1)

        self.wfspan_label = QLabel(f"WF span (+/- Hz): {wf_span_hz}")
        self.wfspan_label.setFont(ctrl_font)
        layout.addWidget(self.wfspan_label, 17, 0)
        self.wfspan_spin = QDoubleSpinBox()
        self.wfspan_spin.setRange(50, sample_rate / 2)
        self.wfspan_spin.setSingleStep(50)
        self.wfspan_spin.setValue(wf_span_hz)
        layout.addWidget(self.wfspan_spin, 17, 1)

        self.quit_button = QPushButton("Quit")
        self.quit_button.pressed.connect(self.end_program)
        layout.addWidget(self.quit_button, 25, 0, 1, 2)

        # Waterfall sliders
        f.setPointSize(12)
        self.low_slider = QSlider(Qt.Horizontal)
        self.low_slider.setMinimum(-100)
        self.low_slider.setMaximum(0)
        self.low_slider.setValue(WATER_LOW_DB)
        self.low_slider.valueChanged.connect(self._update_water_labels)
        layout.addWidget(self.low_slider, 20, 0)

        self.high_slider = QSlider(Qt.Horizontal)
        self.high_slider.setMinimum(-100)
        self.high_slider.setMaximum(0)
        self.high_slider.setValue(WATER_HIGH_DB)
        self.high_slider.valueChanged.connect(self._update_water_labels)
        layout.addWidget(self.high_slider, 21, 0)

        self.low_label = QLabel(f"LOW LEVEL: {WATER_LOW_DB}")
        self.low_label.setFont(f)
        layout.addWidget(self.low_label, 20, 1)
        self.high_label = QLabel(f"HIGH LEVEL: {WATER_HIGH_DB}")
        self.high_label.setFont(f)
        layout.addWidget(self.high_label, 21, 1)

        # Save buffer button
        self.save_button = QPushButton("Save latest buffer (.npz)")
        self.save_button.pressed.connect(self._save_buffer)
        layout.addWidget(self.save_button, 22, 0, 1, 2)

        # FFT plot
        title_style = {"size": "18pt"}
        label_style = {"color": "#FFF", "font-size": "12pt"}
        self.fft_plot = pg.plot()
        self.fft_curve = self.fft_plot.plot(freq, pen={"color": "y", "width": 2})
        self.fft_plot.setLabel("bottom", text="Frequency", units="Hz", **label_style)
        self.fft_plot.setLabel("left", text="Magnitude", units="dB", **label_style)
        self.fft_plot.setTitle("Received Signal – Spectrum", **title_style)
        self.fft_plot.setYRange(-110, 0)
        self.fft_plot.setXRange(signal_freq - fft_span_hz, signal_freq + fft_span_hz)
        layout.addWidget(self.fft_plot, 0, 2, 10, 1)

        # Waterfall
        self.waterfall = pg.PlotWidget()
        self.imageitem = pg.ImageItem()
        self.waterfall.addItem(self.imageitem)
        pos = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
        color = np.array([[68, 1, 84, 255], [59, 82, 139, 255], [33, 145, 140, 255],
                          [94, 201, 98, 255], [253, 231, 37, 255]], dtype=np.ubyte)
        lut = pg.ColorMap(pos, color).getLookupTable(0.0, 1.0, 256)
        self.imageitem.setLookupTable(lut)
        tr = QtGui.QTransform()
        tr.translate(0, -sample_rate / 2)
        tr.scale(UPDATE_PERIOD_S, sample_rate / N)  # x=time, y=frequency
        self.imageitem.setTransform(tr)
        self.waterfall.setRange(yRange=(signal_freq - wf_span_hz, signal_freq + wf_span_hz))
        self.waterfall.setLabel("left", "Frequency", units="Hz", **label_style)
        self.waterfall.setLabel("bottom", "Time", units="sec", **label_style)
        self.waterfall.setTitle("Waterfall Spectrum", **title_style)
        layout.addWidget(self.waterfall, 10, 2, 10, 1)

        # Heartbeat time plot (baseband magnitude, AC coupled)
        self.audio_plot = pg.plot()
        self.audio_plot.setLabel("bottom", "Time", units="s", **label_style)
        self.audio_plot.setLabel("left", "Magnitude", units="linear", **label_style)
        self.audio_plot.setTitle("Heartbeat Envelope (|IQ|, AC-coupled)", **title_style)
        self.audio_curve = self.audio_plot.plot(pen={"color": "m", "width": 2})
        self.audio_plot.setXRange(0, time_axis_bb[-1])
        layout.addWidget(self.audio_plot, 0, 3, 20, 1)

        widget.setLayout(layout)
        self.setCentralWidget(widget)

    def _update_water_labels(self):
        if self.low_slider.value() > self.high_slider.value():
            self.low_slider.setValue(self.high_slider.value())
        self.low_label.setText(f"LOW LEVEL: {self.low_slider.value():0.0f}")
        self.high_label.setText(f"HIGH LEVEL: {self.high_slider.value():0.0f}")

    def apply_settings(self):
        """Apply GUI settings to signal frequency and RF out."""
        global signal_freq, fc, rf_freq, lo_freq
        global rx_gain_ch0, rx_gain_ch1, tx_gain_ch1, tx_scale_ch1
        global bb_decim, view_fs, time_axis_bb, history_len, bb_hist
        global fft_span_hz, wf_span_hz
        global mix_exp, win_funct
        global sample_rate, fft_size, fs, N, ts, freq

        # --- base frequencies ---
        signal_freq = float(self.sig_spin.value() * 1e3)
        self.sig_label.setText(f"Signal freq (kHz): {signal_freq/1e3:0.1f}")

        sample_rate = max(0.521e6, float(self.sr_spin.value()) * 1e6)
        self.sr_spin.setValue(sample_rate / 1e6)
        my_sdr.sample_rate = int(sample_rate)
        fs = int(my_sdr.sample_rate)
        ts = 1 / float(fs)
        self.sr_label.setText(f"Sample rate (MS/s): {sample_rate/1e6:0.3f}")

        fft_size = int(self.fftsize_spin.value())
        my_sdr.rx_buffer_size = int(fft_size)
        N = int(my_sdr.rx_buffer_size)
        self.fftsize_label.setText(f"FFT size (samples): {fft_size}")

        # --- gains and routes ---
        rx_gain_ch0 = float(self.rxg0_spin.value())
        my_sdr.rx_hardwaregain_chan0 = int(rx_gain_ch0)
        self.rxg0_label.setText(f"RX0 gain (dB): {rx_gain_ch0:0.0f}")

        rx_gain_ch1 = float(self.rxg1_spin.value())
        my_sdr.rx_hardwaregain_chan1 = int(rx_gain_ch1)
        self.rxg1_label.setText(f"RX1 gain (dB): {rx_gain_ch1:0.0f}")

        tx_gain_ch1 = float(self.txg1_spin.value())
        my_sdr.tx_hardwaregain_chan1 = int(tx_gain_ch1)
        self.txg1_label.setText(f"TX1 gain (dB): {tx_gain_ch1:0.0f}")

        tx_scale_ch1 = float(self.txscale1_spin.value())
        self.txscale1_label.setText(f"TX1 scale: {tx_scale_ch1:0.2f}")

        # RF (GHz) within allowed range; compute LO = RF + IF and program PLL
        rf_freq = float(self.lo_spin.value() * 1e9)
        rf_freq = min(max(rf_freq, RF_MIN), RF_MAX)
        lo_freq = rf_freq + center_freq
        my_phaser.frequency = int(lo_freq / 4)
        my_phaser.enable = 0  # latch new PLL settings
        _route_tx_out2_and_lo()
        self.lo_spin.setValue(rf_freq / 1e9)
        self.lo_label.setText(f"RF out (GHz): {rf_freq/1e9:0.4f}  |  LO (GHz): {lo_freq/1e9:0.4f}")

        # Ensure TX path stays enabled
        my_sdr.tx_enabled_channels = [1]
        my_sdr.tx_lo = int(center_freq)
        my_sdr.tx_cyclic_buffer = True
        my_sdr._ctx.set_timeout(0)

        # Rebuild transmit tone after all params are settled
        fc = int(signal_freq / (fs / N)) * (fs / N)
        t_local = np.arange(0, N * ts, ts)
        i_local = np.cos(2 * np.pi * t_local * fc) * 2**14
        q_local = np.sin(2 * np.pi * t_local * fc) * 2**14
        iq_local = (i_local + 1j * q_local).astype(np.complex64)
        _load_tx_tone(iq_local * tx_scale_ch1)

        # Update frequency axis and precomputed windows
        freq = np.linspace(-fs / 2, fs / 2, int(N))
        mix_exp = np.exp(-1j * 2 * np.pi * signal_freq * np.arange(fft_size) / sample_rate).astype(np.complex64)
        win_funct = np.blackman(fft_size)

        # Adjust FFT and waterfall zoom
        self.fftspan_spin.setRange(50, sample_rate / 2)
        self.wfspan_spin.setRange(50, sample_rate / 2)
        fft_span_hz = float(self.fftspan_spin.value())
        wf_span_hz = float(self.wfspan_spin.value())
        self.fftspan_label.setText(f"FFT span (+/- Hz): {fft_span_hz:0.0f}")
        self.wfspan_label.setText(f"WF span (+/- Hz): {wf_span_hz:0.0f}")
        self.fft_plot.setXRange(signal_freq - fft_span_hz, signal_freq + fft_span_hz)
        self.waterfall.setRange(yRange=(signal_freq - wf_span_hz, signal_freq + wf_span_hz))

        # Decimation and derived axes
        bb_decim = int(self.decim_spin.value())
        self.decim_label.setText(f"BB decim: {bb_decim}")
        view_fs = fs / bb_decim

        time_history_sec = float(self.timehist_spin.value())
        self.timehist_label.setText(f"Time window (s): {time_history_sec:0.2f}")
        history_len = max(1, int(time_history_sec * view_fs))
        time_axis_bb = np.arange(history_len) / view_fs
        bb_hist = np.zeros(history_len, dtype=complex)
        self.audio_plot.setXRange(0, time_axis_bb[-1])

        # Rebuild waterfall buffer and its transform if FFT size changed
        self.img_array = np.ones((num_slices, fft_size)) * (-100)
        tr = QtGui.QTransform()
        tr.translate(0, -sample_rate / 2)
        tr.scale(UPDATE_PERIOD_S, sample_rate / N)
        self.imageitem.setTransform(tr)
        # Force an immediate refresh with new settings
        update()


    def end_program(self):
        my_sdr.tx_destroy_buffer()
        self.close()

    def _save_buffer(self):
        if self._latest is None:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"if_snapshot_{stamp}.npz"
        np.savez(fname, iq=self._latest, fs=fs, center_freq=center_freq, signal_freq=signal_freq)


win = Window()
index = 0


def update():
    global index, time_axis_bb, view_fs, bb_decim
    global audio_hp_smooth_sec
    global bb_hist, history_len
    global mix_exp, win_funct  # Use globals
    global freq, sample_rate, fft_size

    rx_out = my_sdr.rx()
    arr = np.array(rx_out, copy=False)
    if arr.ndim > 1:
        data = np.sum(arr, axis=0)
    else:
        data = arr
    data = np.asarray(data).ravel()
    win._latest = data

    # Mix to baseband using precomputed vector
    # Ensure data length matches mix_exp (it should if buffer size is constant)
    if len(data) == len(mix_exp):
        bb = data * mix_exp
    else:
        # Fallback if buffer size fluctuates (rare but safe)
        n = np.arange(len(data))
        bb = data * np.exp(-1j * 2 * np.pi * signal_freq * n / fs)
    
    bb = bb[::bb_decim]

    # Windowed FFT (uses precomputed win_funct if size matches)
    if len(data) == len(win_funct):
        y = data * win_funct
        norm_factor = np.sum(win_funct)
        current_freq = freq  # precomputed
    else:
        w = np.blackman(len(data))
        y = data * w
        norm_factor = np.sum(w)
        current_freq = np.linspace(-fs / 2, fs / 2, len(data))

    sp = np.fft.fftshift(np.fft.fft(y))
    s_mag = np.abs(sp) / norm_factor
    s_mag = np.maximum(s_mag, 10 ** (-15))
    s_dbfs = 20 * np.log10(s_mag / (2 ** 11))

    win.fft_curve.setData(current_freq, s_dbfs)
    
    # Update waterfall
    if win.img_array.shape[1] != len(s_dbfs):
        # Resize waterfall buffer if FFT size changed
        win.img_array = np.ones((num_slices, len(s_dbfs))) * -100
        
    win.img_array = np.roll(win.img_array, 1, axis=0)
    win.img_array[0] = s_dbfs
    win.imageitem.setLevels([win.low_slider.value(), win.high_slider.value()])
    win.imageitem.setImage(win.img_array, autoLevels=False)
    
    # Update transform only if size changed (optimization)
    if win.imageitem.width() != len(s_dbfs):
        # Reset transform to match new width
        tr = QtGui.QTransform()
        tr.translate(0, -sample_rate / 2)
        tr.scale(0.35, sample_rate / len(s_dbfs))
        win.imageitem.setTransform(tr)

    # Time/history buffer
    if len(bb_hist) != history_len:
        bb_hist = np.zeros(history_len, dtype=complex)
    if len(bb) >= history_len:
        bb_hist[:] = bb[-history_len:]
    else:
        shift = len(bb)
        bb_hist = np.roll(bb_hist, -shift)
        bb_hist[-shift:] = bb

    bb_view = bb_hist

    # Heartbeat envelope (baseband magnitude)
    mag = np.abs(bb_view)
    # AC couple via mean subtraction
    mag_ac = mag - np.mean(mag)
    win.audio_curve.setData(time_axis_bb, mag_ac)
    peak_env = max(0.01, float(np.max(np.abs(mag_ac))))
    win.audio_plot.setYRange(-1.2 * peak_env, 1.2 * peak_env)
    win.audio_plot.setXRange(0, time_axis_bb[-1], padding=0)

    if index == 1:
        win.fft_plot.enableAutoRange("xy", False)
        win.audio_plot.enableAutoRange("xy", False)
    index += 1


timer = QtCore.QTimer()
timer.timeout.connect(update)
timer.start(int(UPDATE_PERIOD_S * 1000))

sys.exit(App.exec())
