#!/usr/bin/env python3
# Purpose: CW radar IF scope for CN0566/Pluto. Shows FFT, waterfall, and time‑domain IQ.
# Based on: workspace/radar/pyadi-iio/examples/phaser/PhaserRadarLabs/CW_RADAR_Waterfall.py

import sys
from datetime import datetime

import adi
import numpy as np
import warnings
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QThread, pyqtSignal
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
SAMPLE_RATE = 0.6e6          # Pluto ADC/DAC rate (Hz)
IF_FREQ = 2.2e9              # Pluto LO / IF (Hz)
SIGNAL_TONE = 100e3          # Baseband tone sent by Pluto (Hz)
RF_MIN = 10.0e9              # Allowed RF range (Hz)
RF_MAX = 10.5e9
RF_DEFAULT = 10.2e9          # Desired radiated RF (Hz)
NUM_SLICES = 50              # Waterfall depth
FFT_SIZE = 8192              # Buffer size for FFT/waterfall (smaller for speed)
RX_GAIN_CH0 = 30             # dB, manual gain
RX_GAIN_CH1 = 30
TX_GAIN_CH0 = -88            # dB, –88 = off
TX_GAIN_CH1 = 0
TX_SCALE_CH0 = 0.5           # Relative amplitude for I data fed to TX0
TX_SCALE_CH1 = 1.0           # Relative amplitude for I data fed to TX1
GAIN_TAPER = [8, 34, 84, 127, 127, 84, 34, 8]  # Blackman taper for the 8 RX elems
BB_DECIM = 2                 # Decimation for baseband/time/audio views (lower = wider time span)
RAW_IF_VIEW_MS = 5           # Duration shown in raw IF plot (ms)
FFT_SPAN_HZ = 600            # +/- span around tone for main FFT plot
WF_SPAN_HZ = 300             # +/- span for waterfall axis
WATER_LOW_DB = -66           # Initial waterfall lower level
WATER_HIGH_DB = -42          # Initial waterfall upper level
AUDIO_PLOT_MAX_HZ = 500      # Default x-range for audio/Doppler FFT (Hz)
AUDIO_FFT_LEN = 4096         # FFT length for audio/Doppler view
AUDIO_HP_SMOOTH_SEC = 0.04   # Smoothing window for DC removal in envelope (s)
TIME_HISTORY_SEC = 1.0       # Length of baseband history shown (s)
raw_if_ms_default = RAW_IF_VIEW_MS
audio_plot_max_hz_default = AUDIO_PLOT_MAX_HZ
audio_fft_len_default = AUDIO_FFT_LEN
audio_hp_smooth_sec_default = AUDIO_HP_SMOOTH_SEC
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
tx_gain_ch0 = TX_GAIN_CH0
tx_gain_ch1 = TX_GAIN_CH1
tx_scale_ch0 = TX_SCALE_CH0
tx_scale_ch1 = TX_SCALE_CH1
GAIN_TAPER_RUNTIME = GAIN_TAPER.copy()
bb_decim = bb_decim_default
raw_if_ms = raw_if_ms_default
audio_plot_max_hz = audio_plot_max_hz_default
audio_fft_len = audio_fft_len_default
audio_hp_smooth_sec = audio_hp_smooth_sec_default
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


# Instantiate hardware
my_sdr = adi.ad9361(uri=SDR_IP)
my_phaser = adi.CN0566(uri=RPI_IP, sdr=my_sdr)

# Phaser bring-up
my_phaser.configure(device_mode="rx")
my_phaser.load_gain_cal()
my_phaser.load_phase_cal()
for i in range(8):
    my_phaser.set_chan_phase(i, 0)
for i, g in enumerate(GAIN_TAPER):
    my_phaser.set_chan_gain(i, g, apply_cal=True)
try:
    my_phaser._gpios.gpio_tx_sw = 0
    my_phaser._gpios.gpio_vctrl_1 = 1
    my_phaser._gpios.gpio_vctrl_2 = 1
except AttributeError:
    my_phaser.gpios.gpio_tx_sw = 0
    my_phaser.gpios.gpio_vctrl_1 = 1
    my_phaser.gpios.gpio_vctrl_2 = 1

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
my_sdr.tx_hardwaregain_chan0 = tx_gain_ch0
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
my_sdr.tx(iq * tx_scale_ch1)  # single TX channel (TX1)

freq = np.linspace(-fs / 2, fs / 2, int(N))
time_axis = np.arange(N) / fs
# baseband decimation for time-domain/audio views (keep modest to show motion)
bb_decim = BB_DECIM  # 600 kHz / 6 ≈ 100 kS/s view rate by default
view_fs = fs / bb_decim
if_view_len = int((raw_if_ms / 1000) * fs)
time_axis_if = np.arange(if_view_len) / fs
history_len = int(time_history_sec * view_fs)
time_axis_bb = np.arange(history_len) / view_fs
bb_hist = np.zeros(history_len, dtype=complex)

# Audio/Doppler FFT settings
audio_fft_len = AUDIO_FFT_LEN  # resolution ~ view_fs / audio_fft_len
audio_freq_axis = np.fft.rfftfreq(audio_fft_len, d=1 / view_fs)
audio_plot_max_hz = AUDIO_PLOT_MAX_HZ  # display limit

# Silence numpy complex casting warnings that clutter the console
warnings.filterwarnings("ignore", category=np.exceptions.ComplexWarning)

# Qt application
App = QApplication(sys.argv)


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

        self.audio_span_label = QLabel(f"Audio span (Hz): {audio_plot_max_hz:0.0f}")
        self.audio_span_label.setFont(ctrl_font)
        layout.addWidget(self.audio_span_label, 2, 0, 1, 1)
        self.audio_span_spin = QDoubleSpinBox()
        self.audio_span_spin.setRange(50, view_fs / 2)
        self.audio_span_spin.setSingleStep(50)
        self.audio_span_spin.setValue(audio_plot_max_hz)
        layout.addWidget(self.audio_span_spin, 2, 1, 1, 1)

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

        self.txg0_label = QLabel(f"TX0 gain (dB): {tx_gain_ch0}")
        self.txg0_label.setFont(ctrl_font)
        layout.addWidget(self.txg0_label, 8, 0)
        self.txg0_spin = QDoubleSpinBox()
        self.txg0_spin.setRange(-88, 0)
        self.txg0_spin.setSingleStep(1)
        self.txg0_spin.setValue(tx_gain_ch0)
        layout.addWidget(self.txg0_spin, 8, 1)

        self.txg1_label = QLabel(f"TX1 gain (dB): {tx_gain_ch1}")
        self.txg1_label.setFont(ctrl_font)
        layout.addWidget(self.txg1_label, 9, 0)
        self.txg1_spin = QDoubleSpinBox()
        self.txg1_spin.setRange(-88, 0)
        self.txg1_spin.setSingleStep(1)
        self.txg1_spin.setValue(tx_gain_ch1)
        layout.addWidget(self.txg1_spin, 9, 1)

        self.txscale0_label = QLabel(f"TX0 scale: {tx_scale_ch0:0.2f}")
        self.txscale0_label.setFont(ctrl_font)
        layout.addWidget(self.txscale0_label, 10, 0)
        self.txscale0_spin = QDoubleSpinBox()
        self.txscale0_spin.setRange(0.0, 1.0)
        self.txscale0_spin.setSingleStep(0.05)
        self.txscale0_spin.setValue(tx_scale_ch0)
        layout.addWidget(self.txscale0_spin, 10, 1)

        self.txscale1_label = QLabel(f"TX1 scale: {tx_scale_ch1:0.2f}")
        self.txscale1_label.setFont(ctrl_font)
        layout.addWidget(self.txscale1_label, 11, 0)
        self.txscale1_spin = QDoubleSpinBox()
        self.txscale1_spin.setRange(0.0, 1.0)
        self.txscale1_spin.setSingleStep(0.05)
        self.txscale1_spin.setValue(tx_scale_ch1)
        layout.addWidget(self.txscale1_spin, 11, 1)

        # Decimation and spans
        self.decim_label = QLabel(f"BB decim: {bb_decim}")
        self.decim_label.setFont(ctrl_font)
        layout.addWidget(self.decim_label, 12, 0)
        self.decim_spin = QDoubleSpinBox()
        self.decim_spin.setRange(1, 20)
        self.decim_spin.setDecimals(0)
        self.decim_spin.setSingleStep(1)
        self.decim_spin.setValue(bb_decim)
        layout.addWidget(self.decim_spin, 12, 1)

        self.timehist_label = QLabel(f"Time window (s): {time_history_sec:0.2f}")
        self.timehist_label.setFont(ctrl_font)
        layout.addWidget(self.timehist_label, 13, 0)
        self.timehist_spin = QDoubleSpinBox()
        self.timehist_spin.setRange(0.05, 10.0)
        self.timehist_spin.setSingleStep(0.05)
        self.timehist_spin.setValue(time_history_sec)
        layout.addWidget(self.timehist_spin, 13, 1)

        self.ifms_label = QLabel(f"Raw IF window (ms): {raw_if_ms}")
        self.ifms_label.setFont(ctrl_font)
        layout.addWidget(self.ifms_label, 14, 0)
        self.ifms_spin = QDoubleSpinBox()
        self.ifms_spin.setRange(1, 20)
        self.ifms_spin.setDecimals(0)
        self.ifms_spin.setSingleStep(1)
        self.ifms_spin.setValue(raw_if_ms)
        layout.addWidget(self.ifms_spin, 14, 1)

        self.fftspan_label = QLabel(f"FFT span (+/- Hz): {fft_span_hz}")
        self.fftspan_label.setFont(ctrl_font)
        layout.addWidget(self.fftspan_label, 15, 0)
        self.fftspan_spin = QDoubleSpinBox()
        self.fftspan_spin.setRange(50, sample_rate / 2)
        self.fftspan_spin.setSingleStep(50)
        self.fftspan_spin.setValue(fft_span_hz)
        layout.addWidget(self.fftspan_spin, 15, 1)

        self.wfspan_label = QLabel(f"WF span (+/- Hz): {wf_span_hz}")
        self.wfspan_label.setFont(ctrl_font)
        layout.addWidget(self.wfspan_label, 16, 0)
        self.wfspan_spin = QDoubleSpinBox()
        self.wfspan_spin.setRange(50, sample_rate / 2)
        self.wfspan_spin.setSingleStep(50)
        self.wfspan_spin.setValue(wf_span_hz)
        layout.addWidget(self.wfspan_spin, 16, 1)

        self.audiosmooth_label = QLabel(f"Audio HP smooth (ms): {audio_hp_smooth_sec*1000:0.0f}")
        self.audiosmooth_label.setFont(ctrl_font)
        layout.addWidget(self.audiosmooth_label, 17, 0)
        self.audiosmooth_spin = QDoubleSpinBox()
        self.audiosmooth_spin.setRange(5, 500)
        self.audiosmooth_spin.setSingleStep(5)
        self.audiosmooth_spin.setValue(audio_hp_smooth_sec * 1000)
        layout.addWidget(self.audiosmooth_spin, 17, 1)

        self.audiofft_label = QLabel(f"Audio FFT len: {audio_fft_len}")
        self.audiofft_label.setFont(ctrl_font)
        layout.addWidget(self.audiofft_label, 18, 0)
        self.audiofft_spin = QDoubleSpinBox()
        self.audiofft_spin.setRange(1024, 16384)
        self.audiofft_spin.setDecimals(0)
        self.audiofft_spin.setSingleStep(1024)
        self.audiofft_spin.setValue(audio_fft_len)
        layout.addWidget(self.audiofft_spin, 18, 1)

        self.quit_button = QPushButton("Quit")
        self.quit_button.pressed.connect(self.end_program)
        layout.addWidget(self.quit_button, 24, 0, 1, 2)

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
        self.fft_plot.setYRange(-70, 0)
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
        tr.scale(0.35, sample_rate / N)
        self.imageitem.setTransform(tr)
        self.waterfall.setRange(yRange=(signal_freq - wf_span_hz, signal_freq + wf_span_hz))
        self.waterfall.setLabel("left", "Frequency", units="Hz", **label_style)
        self.waterfall.setLabel("bottom", "Time", units="sec", **label_style)
        self.waterfall.setTitle("Waterfall Spectrum", **title_style)
        layout.addWidget(self.waterfall, 10, 2, 10, 1)

        # Time-domain plot (baseband)
        self.time_plot = pg.plot()
        self.time_plot.setLabel("bottom", "Time", units="s", **label_style)
        self.time_plot.setLabel("left", "IQ (FS units)", **label_style)
        self.time_plot.setTitle("Baseband Time Trace (I=yellow, Q=cyan)", **title_style)
        self.time_curve_i = self.time_plot.plot(time_axis_bb, np.zeros_like(time_axis_bb),
                                                pen={"color": "y", "width": 1})
        self.time_curve_q = self.time_plot.plot(time_axis_bb, np.zeros_like(time_axis_bb),
                                                pen={"color": "c", "width": 1})
        self.time_plot.setXRange(0, time_axis_bb[-1])
        self.time_plot.enableAutoRange(False, False)
        layout.addWidget(self.time_plot, 0, 3, 10, 1)

        # Raw IF time plot (short window, unmixed)
        self.if_plot = pg.plot()
        self.if_plot.setLabel("bottom", "Time", units="s", **label_style)
        self.if_plot.setLabel("left", "IQ (FS units)", **label_style)
        self.if_plot.setTitle("Raw IF Trace (I=yellow, Q=cyan)", **title_style)
        self.if_curve_i = self.if_plot.plot(time_axis_if, np.zeros_like(time_axis_if),
                                            pen={"color": "y", "width": 1})
        self.if_curve_q = self.if_plot.plot(time_axis_if, np.zeros_like(time_axis_if),
                                            pen={"color": "c", "width": 1})
        self.if_plot.setXRange(0, time_axis_if[-1])
        self.if_plot.enableAutoRange(False, False)
        layout.addWidget(self.if_plot, 10, 3, 10, 1)

        # Magnitude envelope (audio) plot
        self.audio_plot = pg.plot()
        self.audio_plot.setLabel("bottom", "Time", units="s", **label_style)
        self.audio_plot.setLabel("left", "Magnitude", units="dBFS", **label_style)
        self.audio_plot.setTitle("Audio Envelope (|IQ|, hp ~8 Hz)", **title_style)
        self.audio_curve = self.audio_plot.plot(pen={"color": "m", "width": 2})
        layout.addWidget(self.audio_plot, 20, 3, 10, 1)

        # Audio/Doppler FFT plot
        self.audio_fft_plot = pg.plot()
        self.audio_fft_plot.setLabel("bottom", "Frequency", units="Hz", **label_style)
        self.audio_fft_plot.setLabel("left", "Magnitude", units="dB", **label_style)
        self.audio_fft_plot.setTitle("Audio/Doppler FFT (baseband)", **title_style)
        self.audio_fft_curve = self.audio_fft_plot.plot(pen={"color": "g", "width": 2})
        self.audio_fft_plot.setXRange(0, audio_plot_max_hz)
        self.audio_fft_plot.setYRange(-80, 5)
        layout.addWidget(self.audio_fft_plot, 20, 2, 10, 1)

        widget.setLayout(layout)
        self.setCentralWidget(widget)

    def _update_water_labels(self):
        if self.low_slider.value() > self.high_slider.value():
            self.low_slider.setValue(self.high_slider.value())
        self.low_label.setText(f"LOW LEVEL: {self.low_slider.value():0.0f}")
        self.high_label.setText(f"HIGH LEVEL: {self.high_slider.value():0.0f}")

    def apply_settings(self):
        """Apply GUI settings to signal frequency, audio span, and RF out."""
        global signal_freq, fc, rf_freq, lo_freq, audio_plot_max_hz
        global rx_gain_ch0, rx_gain_ch1, tx_gain_ch0, tx_gain_ch1
        global tx_scale_ch0, tx_scale_ch1
        global bb_decim, view_fs, time_axis_bb, history_len, bb_hist
        global raw_if_ms, if_view_len, time_axis_if
        global fft_span_hz, wf_span_hz
        global audio_hp_smooth_sec, audio_fft_len, audio_freq_axis

        # Signal frequency (kHz -> Hz)
        signal_freq = float(self.sig_spin.value() * 1e3)
        self.sig_label.setText(f"Signal freq (kHz): {signal_freq/1e3:0.1f}")

        # Rebuild transmit tone
        fc = int(signal_freq / (fs / N)) * (fs / N)
        t_local = np.arange(0, N * ts, ts)
        i_local = np.cos(2 * np.pi * t_local * fc) * 2**14
        q_local = np.sin(2 * np.pi * t_local * fc) * 2**14
        iq_local = (i_local + 1j * q_local).astype(np.complex64)
        my_sdr.tx_destroy_buffer()          # ensure cyclic buffer reloads new tone
        my_sdr.tx(iq_local * tx_scale_ch1)  # single TX channel (TX1)

        # Gains (only RX0 and TX1 are active; TX0 settings kept for completeness)
        rx_gain_ch0 = float(self.rxg0_spin.value())
        my_sdr.rx_hardwaregain_chan0 = int(rx_gain_ch0)
        self.rxg0_label.setText(f"RX0 gain (dB): {rx_gain_ch0:0.0f}")

        rx_gain_ch1 = float(self.rxg1_spin.value())
        my_sdr.rx_hardwaregain_chan1 = int(rx_gain_ch1)
        self.rxg1_label.setText(f"RX1 gain (dB): {rx_gain_ch1:0.0f}")

        tx_gain_ch0 = float(self.txg0_spin.value())
        my_sdr.tx_hardwaregain_chan0 = int(tx_gain_ch0)
        self.txg0_label.setText(f"TX0 gain (dB): {tx_gain_ch0:0.0f}")

        tx_gain_ch1 = float(self.txg1_spin.value())
        my_sdr.tx_hardwaregain_chan1 = int(tx_gain_ch1)
        self.txg1_label.setText(f"TX1 gain (dB): {tx_gain_ch1:0.0f}")

        tx_scale_ch0 = float(self.txscale0_spin.value())
        tx_scale_ch1 = float(self.txscale1_spin.value())
        self.txscale0_label.setText(f"TX0 scale: {tx_scale_ch0:0.2f}")
        self.txscale1_label.setText(f"TX1 scale: {tx_scale_ch1:0.2f}")

        # Adjust FFT and waterfall zoom
        fft_span_hz = float(self.fftspan_spin.value())
        wf_span_hz = float(self.wfspan_spin.value())
        self.fftspan_label.setText(f"FFT span (+/- Hz): {fft_span_hz:0.0f}")
        self.wfspan_label.setText(f"WF span (+/- Hz): {wf_span_hz:0.0f}")
        self.fft_plot.setXRange(signal_freq - fft_span_hz, signal_freq + fft_span_hz)
        self.waterfall.setRange(yRange=(signal_freq - wf_span_hz, signal_freq + wf_span_hz))

        # RF (GHz) within allowed range; compute LO = RF + IF and program PLL
        rf_freq = float(self.lo_spin.value() * 1e9)
        rf_freq = min(max(rf_freq, RF_MIN), RF_MAX)
        lo_freq = rf_freq + center_freq
        my_phaser.frequency = int(lo_freq / 4)
        self.lo_spin.setValue(rf_freq / 1e9)
        self.lo_label.setText(f"RF out (GHz): {rf_freq/1e9:0.4f}  |  LO (GHz): {lo_freq/1e9:0.4f}")

        # Decimation and derived axes
        bb_decim = int(self.decim_spin.value())
        self.decim_label.setText(f"BB decim: {bb_decim}")
        view_fs = fs / bb_decim
        time_history_sec = float(self.timehist_spin.value())
        self.timehist_label.setText(f"Time window (s): {time_history_sec:0.2f}")
        history_len = max(1, int(time_history_sec * view_fs))
        time_axis_bb = np.arange(history_len) / view_fs
        bb_hist = np.zeros(history_len, dtype=complex)
        self.time_plot.setXRange(0, time_axis_bb[-1])

        # Raw IF window
        raw_if_ms = float(self.ifms_spin.value())
        self.ifms_label.setText(f"Raw IF window (ms): {raw_if_ms:0.0f}")
        if_view_len = int((raw_if_ms / 1000) * fs)
        time_axis_if = np.arange(if_view_len) / fs
        self.if_plot.setXRange(0, time_axis_if[-1])

        # Audio span (update max based on new view_fs)
        self.audio_span_spin.setMaximum(view_fs / 2)
        current_span = float(self.audio_span_spin.value())
        audio_plot_max_hz = min(current_span, view_fs / 2)
        self.audio_span_spin.setValue(audio_plot_max_hz)
        self.audio_span_label.setText(f"Audio span (Hz): {audio_plot_max_hz:0.0f}")
        self.audio_fft_plot.setXRange(0, audio_plot_max_hz)

        # Audio smoothing (ms -> s)
        audio_hp_smooth_sec = float(self.audiosmooth_spin.value()) / 1000.0
        self.audiosmooth_label.setText(f"Audio HP smooth (ms): {self.audiosmooth_spin.value():0.0f}")

        # Audio FFT length
        audio_fft_len = int(self.audiofft_spin.value())

        # Recompute audio frequency axis after decim/fft length changes
        audio_freq_axis = np.fft.rfftfreq(audio_fft_len, d=1 / view_fs)
        self.audiofft_label.setText(f"Audio FFT len: {audio_fft_len}")


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


def update(rx_out):
    global index, time_axis_bb, time_axis_if, view_fs, bb_decim
    global audio_freq_axis, audio_fft_len, audio_hp_smooth_sec
    global bb_hist, history_len
    arr = np.array(rx_out, copy=False)
    if arr.ndim > 1:
        data = np.sum(arr, axis=0)
    else:
        data = arr
    data = np.asarray(data).ravel()
    win._latest = data

    # Mix to baseband and decimate for readable time-domain views
    n = np.arange(len(data))
    bb = data * np.exp(-1j * 2 * np.pi * signal_freq * n / fs)
    bb = bb[::bb_decim]

    # Windowed FFT
    win_funct = np.blackman(len(data))
    y = data * win_funct
    sp = np.fft.fftshift(np.fft.fft(y))
    s_mag = np.abs(sp) / np.sum(win_funct)
    s_mag = np.maximum(s_mag, 10 ** (-15))
    s_dbfs = 20 * np.log10(s_mag / (2 ** 11))

    win.fft_curve.setData(freq, s_dbfs)
    win.img_array = np.roll(win.img_array, 1, axis=0)
    win.img_array[0] = s_dbfs
    win.imageitem.setLevels([win.low_slider.value(), win.high_slider.value()])
    win.imageitem.setImage(win.img_array, autoLevels=False)

    # Time/history buffer
    if len(bb_hist) != history_len:
        bb_hist = np.zeros(history_len, dtype=complex)
    if len(bb) >= history_len:
        bb_hist = bb[-history_len:]
    else:
        bb_hist = np.zeros(history_len, dtype=complex)
        bb_hist[-len(bb):] = bb

    bb_view = bb_hist
    win.time_curve_i.setData(time_axis_bb, np.real(bb_view))
    win.time_curve_q.setData(time_axis_bb, np.imag(bb_view))
    peak = max(1.0, float(np.max(np.abs(bb_view))))
    win.time_plot.setYRange(-1.2 * peak, 1.2 * peak)
    win.time_plot.setXRange(0, time_axis_bb[-1], padding=0)

    # Raw IF plot (first 5 ms)
    if len(data) >= if_view_len:
        if_view = data[:if_view_len]
        win.if_curve_i.setData(time_axis_if, np.real(if_view))
        win.if_curve_q.setData(time_axis_if, np.imag(if_view))
        if_peak = max(1.0, float(np.max(np.abs(if_view))))
        win.if_plot.setYRange(-1.2 * if_peak, 1.2 * if_peak)

        # Audio envelope (HP ~8 Hz on baseband magnitude)
        mag = np.abs(bb_view)
        taps = max(20, int(audio_hp_smooth_sec * view_fs))  # smoothing for DC removal
        dc = np.convolve(mag, np.ones(taps) / taps, mode="same")
        audio = 20 * np.log10(np.maximum(mag - dc, 1e-9))
        win.audio_curve.setData(time_axis_bb, audio)

        # Audio/Doppler FFT (0 to audio_plot_max_hz), DC-removed, normalized
        bb_short = bb_view
        if len(bb_short) < audio_fft_len:
            bb_short = np.pad(bb_short, (0, audio_fft_len - len(bb_short)), "constant")
        else:
            bb_short = bb_short[:audio_fft_len]
        bb_short = bb_short - np.mean(bb_short)
        window = np.hanning(len(bb_short))
        aud_spec = np.fft.rfft(bb_short * window)
        aud_mag = np.abs(aud_spec) / (np.sum(window) / 2)
        aud_mag_db = 20 * np.log10(np.maximum(aud_mag, 1e-12))
        aud_mag_db -= np.max(aud_mag_db)  # normalize peak to 0 dB
        mask = audio_freq_axis <= audio_plot_max_hz
        win.audio_fft_curve.setData(audio_freq_axis[mask], aud_mag_db[mask])

    if index == 1:
        win.fft_plot.enableAutoRange("xy", False)
        win.time_plot.enableAutoRange("xy", False)
    index += 1


class _RxWorker(QThread):
    data_ready = pyqtSignal(object)

    def run(self):
        while not self.isInterruptionRequested():
            try:
                self.data_ready.emit(my_sdr.rx())
            except Exception as exc:
                print(f"[RX] {exc}")
                break


_rx = _RxWorker()
_rx.data_ready.connect(update)
_rx.start()

sys.exit(App.exec())
