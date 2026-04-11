#!/usr/bin/env python3
"""Full-band FMCW FFT + waterfall for CN0566 (10.0-10.5 GHz sweep).

This variant keeps the simple waterfall UI but uses the same mixer math as the
newer range-Doppler code:

  radiated_rf ~= pll_lo - (center_freq + signal_freq)

So we program the ADF4159 PLL above the desired X-band RF center instead of
treating the radiated RF as the PLL frequency directly.
"""

import sys

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QCheckBox, QGridLayout, QLabel, \
    QMainWindow, QPushButton, QSlider, QWidget
from pyqtgraph.Qt import QtGui

import adi


# ---------------------------------------------------------------------------
# Full-band parameters (antenna-optimized)
# ---------------------------------------------------------------------------
RPI_IP = "ip:phaser.local"
SDR_IP = "ip:phaser.local:50901"

SAMPLE_RATE = 4e6
CENTER_FREQ = 2.1e9
SIGNAL_FREQ = 100e3
NUM_SLICES = 400
FFT_SIZE = 4096

RF_OUTPUT_FREQ = 10.25e9
CHIRP_BW = 500e6
NUM_STEPS = 500
RAMP_TIME_US = 500

GAIN_LIST = [8, 34, 84, 127, 127, 84, 34, 8]
PLOT_FREQ = 200e3


# ---------------------------------------------------------------------------
# Hardware bring-up
# ---------------------------------------------------------------------------
print("Connecting to hardware...")
my_sdr = adi.ad9361(uri=SDR_IP)
my_sdr._ctx.set_timeout(0)
my_phaser = adi.CN0566(uri=RPI_IP, sdr=my_sdr)

my_phaser.configure(device_mode="rx")
my_phaser.load_gain_cal()
my_phaser.load_phase_cal()
for i in range(8):
    my_phaser.set_chan_phase(i, 0)
for i, gain in enumerate(GAIN_LIST):
    my_phaser.set_chan_gain(i, gain, apply_cal=True)

try:
    my_phaser._gpios.gpio_tx_sw = 0
    my_phaser._gpios.gpio_vctrl_1 = 1
    my_phaser._gpios.gpio_vctrl_2 = 1
except AttributeError:
    my_phaser.gpios.gpio_tx_sw = 0
    my_phaser.gpios.gpio_vctrl_1 = 1
    my_phaser.gpios.gpio_vctrl_2 = 1

my_sdr.sample_rate = int(SAMPLE_RATE)
my_sdr.rx_lo = int(CENTER_FREQ)
my_sdr.rx_enabled_channels = [0, 1]
my_sdr.rx_buffer_size = int(FFT_SIZE)
my_sdr.gain_control_mode_chan0 = "manual"
my_sdr.gain_control_mode_chan1 = "manual"
my_sdr.rx_hardwaregain_chan0 = 30
my_sdr.rx_hardwaregain_chan1 = 30

my_sdr.tx_lo = int(CENTER_FREQ)
my_sdr.tx_enabled_channels = [0, 1]
my_sdr.tx_cyclic_buffer = True
my_sdr.tx_hardwaregain_chan0 = -88
my_sdr.tx_hardwaregain_chan1 = 0

ramp_time_s = RAMP_TIME_US / 1e6
PLL_OUTPUT_FREQ = RF_OUTPUT_FREQ + CENTER_FREQ + SIGNAL_FREQ
my_phaser.frequency = int(PLL_OUTPUT_FREQ / 4)
my_phaser.freq_dev_range = int(CHIRP_BW / 4)
my_phaser.freq_dev_step = int((CHIRP_BW / 4) / NUM_STEPS)
my_phaser.freq_dev_time = int(RAMP_TIME_US)
my_phaser.delay_word = 4095
my_phaser.delay_clk = "PFD"
my_phaser.delay_start_en = 0
my_phaser.ramp_delay_en = 0
my_phaser.trig_delay_en = 0
my_phaser.ramp_mode = "continuous_triangular"
my_phaser.sing_ful_tri = 0
my_phaser.tx_trig_en = 0
my_phaser.enable = 0

fs = int(my_sdr.sample_rate)
N = int(my_sdr.rx_buffer_size)
fc = int(SIGNAL_FREQ / (fs / N)) * (fs / N)
ts = 1 / float(fs)
t = np.arange(0, N * ts, ts)
i = np.cos(2 * np.pi * t * fc) * 2**14
q = np.sin(2 * np.pi * t * fc) * 2**14
iq = i + 1j * q

try:
    my_sdr.tx_destroy_buffer()
except Exception:
    pass
my_sdr.tx([iq * 0.5, iq])

print(
    "[fullband] rf_center=%.4fGHz pll_center=%.4fGHz sample_rate=%.1fMHz "
    "ramp=%dus bw=%.0fMHz"
    % (
        RF_OUTPUT_FREQ / 1e9,
        PLL_OUTPUT_FREQ / 1e9,
        SAMPLE_RATE / 1e6,
        RAMP_TIME_US,
        CHIRP_BW / 1e6,
    )
)


# ---------------------------------------------------------------------------
# Derived axes
# ---------------------------------------------------------------------------
c = 3e8
plot_dist = False
freq = np.linspace(-fs / 2, fs / 2, N)
slope = CHIRP_BW / ramp_time_s
dist = (freq - SIGNAL_FREQ) * c / (4 * slope)

clutter_estimate = None
CLUTTER_ALPHA = 0.95


class RxWorker(QThread):
    data_ready = pyqtSignal(object)

    def run(self):
        for _ in range(3):
            try:
                my_sdr.rx()
            except Exception:
                pass
        while not self.isInterruptionRequested():
            try:
                data = my_sdr.rx()
                self.data_ready.emit(data)
            except Exception as exc:
                print(f"[RX] {exc}")
                break


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Full-band RADAR FFT Waterfall")
        self.setMinimumWidth(1500)
        self.img_array = np.ones((NUM_SLICES, FFT_SIZE)) * (-100)
        self.frame_index = 0
        self._build_ui()
        self.showMaximized()

    def _build_ui(self):
        widget = QWidget()
        layout = QGridLayout()

        title = QLabel("PHASER Full-band FMCW Radar")
        font = title.font()
        font.setPointSize(24)
        title.setFont(font)
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title, 0, 0, 1, 2)

        font.setPointSize(10)
        self.x_axis_check = QCheckBox("Convert to Distance")
        self.x_axis_check.setFont(font)
        self.x_axis_check.stateChanged.connect(self.change_x_axis)
        layout.addWidget(self.x_axis_check, 2, 0)

        self.range_res_label = QLabel(
            "B_RF: %0.2f MHz - R_res: %0.2f m" % (CHIRP_BW / 1e6, c / (2 * CHIRP_BW))
        )
        self.range_res_label.setFont(font)
        layout.addWidget(self.range_res_label, 4, 1)

        self.bw_slider = QSlider(Qt.Horizontal)
        self.bw_slider.setMinimum(100)
        self.bw_slider.setMaximum(500)
        self.bw_slider.setValue(int(CHIRP_BW / 1e6))
        self.bw_slider.setTickInterval(50)
        self.bw_slider.setTickPosition(QSlider.TicksBelow)
        self.bw_slider.valueChanged.connect(self.get_range_res)
        layout.addWidget(self.bw_slider, 4, 0)

        self.set_bw = QPushButton("Set RF Bandwidth")
        self.set_bw.pressed.connect(self.set_range_res)
        layout.addWidget(self.set_bw, 5, 0, 1, 1)

        self.low_slider = QSlider(Qt.Horizontal)
        self.low_slider.setMinimum(-100)
        self.low_slider.setMaximum(0)
        self.low_slider.setValue(-45)
        self.low_slider.setTickInterval(20)
        self.low_slider.setTickPosition(QSlider.TicksBelow)
        self.low_slider.valueChanged.connect(self.get_water_levels)
        layout.addWidget(self.low_slider, 8, 0)

        self.high_slider = QSlider(Qt.Horizontal)
        self.high_slider.setMinimum(-100)
        self.high_slider.setMaximum(0)
        self.high_slider.setValue(-25)
        self.high_slider.setTickInterval(20)
        self.high_slider.setTickPosition(QSlider.TicksBelow)
        self.high_slider.valueChanged.connect(self.get_water_levels)
        layout.addWidget(self.high_slider, 10, 0)

        self.low_label = QLabel(f"LOW LEVEL: {self.low_slider.value():0.0f}")
        self.low_label.setFont(font)
        layout.addWidget(self.low_label, 8, 1)

        self.high_label = QLabel(f"HIGH LEVEL: {self.high_slider.value():0.0f}")
        self.high_label.setFont(font)
        layout.addWidget(self.high_label, 10, 1)

        self.steer_slider = QSlider(Qt.Horizontal)
        self.steer_slider.setMinimum(-80)
        self.steer_slider.setMaximum(80)
        self.steer_slider.setValue(0)
        self.steer_slider.setTickInterval(20)
        self.steer_slider.setTickPosition(QSlider.TicksBelow)
        self.steer_slider.valueChanged.connect(self.get_steer_angle)
        layout.addWidget(self.steer_slider, 14, 0)

        self.steer_label = QLabel("0 DEG")
        self.steer_label.setFont(font)
        layout.addWidget(self.steer_label, 14, 1, 1, 2)

        self.quit_button = QPushButton("Quit")
        self.quit_button.pressed.connect(self.end_program)
        layout.addWidget(self.quit_button, 30, 0, 2, 2)

        self.fft_plot = pg.plot()
        self.fft_curve = self.fft_plot.plot(freq, pen={"color": "y", "width": 2})
        title_style = {"size": "20pt"}
        label_style = {"color": "#FFF", "font-size": "14pt"}
        self.fft_plot.setLabel("bottom", text="Frequency", units="Hz", **label_style)
        self.fft_plot.setLabel("left", text="Magnitude", units="dB", **label_style)
        self.fft_plot.setTitle("Received Signal - Frequency Spectrum", **title_style)
        self.fft_plot.setYRange(-60, 0)
        self.fft_plot.setXRange(SIGNAL_FREQ - PLOT_FREQ, SIGNAL_FREQ + PLOT_FREQ)
        layout.addWidget(self.fft_plot, 0, 2, 12, 1)

        self.waterfall = pg.PlotWidget()
        self.imageitem = pg.ImageItem()
        self.waterfall.addItem(self.imageitem)
        pos = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
        color = np.array([
            [68, 1, 84, 255],
            [59, 82, 139, 255],
            [33, 145, 140, 255],
            [94, 201, 98, 255],
            [253, 231, 37, 255],
        ], dtype=np.ubyte)
        lut = pg.ColorMap(pos, color).getLookupTable(0.0, 1.0, 256)
        self.imageitem.setLookupTable(lut)
        tr = QtGui.QTransform()
        tr.translate(0, -SAMPLE_RATE / 2)
        tr.scale(0.35, SAMPLE_RATE / N)
        self.imageitem.setTransform(tr)
        self.waterfall.setRange(yRange=(SIGNAL_FREQ - 20e3, SIGNAL_FREQ + 20e3))
        self.waterfall.setTitle("Waterfall Spectrum", **title_style)
        self.waterfall.setLabel("left", "Frequency", units="Hz", **label_style)
        self.waterfall.setLabel("bottom", "Time", units="sec", **label_style)
        layout.addWidget(self.waterfall, 13, 2, 12, 1)

        widget.setLayout(layout)
        self.setCentralWidget(widget)

    def get_range_res(self):
        bw = self.bw_slider.value() * 1e6
        self.range_res_label.setText(
            "B_RF: %0.2f MHz - R_res: %0.2f m" % (bw / 1e6, c / (2 * bw))
        )

    def get_water_levels(self):
        if self.low_slider.value() > self.high_slider.value():
            self.low_slider.setValue(self.high_slider.value())
        self.low_label.setText(f"LOW LEVEL: {self.low_slider.value():0.0f}")
        self.high_label.setText(f"HIGH LEVEL: {self.high_slider.value():0.0f}")

    def get_steer_angle(self):
        self.steer_label.setText(f"{self.steer_slider.value():0.0f} DEG")
        phase_delta = (
            2 * np.pi * RF_OUTPUT_FREQ * 0.014
            * np.sin(np.radians(self.steer_slider.value())) / c
        )
        my_phaser.set_beam_phase_diff(np.degrees(phase_delta))

    def set_range_res(self):
        global slope, dist
        bw = self.bw_slider.value() * 1e6
        slope = bw / ramp_time_s
        dist = (freq - SIGNAL_FREQ) * c / (4 * slope)
        if self.x_axis_check.isChecked():
            self.fft_plot.setXRange(0, PLOT_FREQ * c / (4 * slope))
        else:
            self.fft_plot.setXRange(SIGNAL_FREQ - PLOT_FREQ, SIGNAL_FREQ + PLOT_FREQ)
        my_phaser.freq_dev_range = int(bw / 4)
        my_phaser.freq_dev_step = int((bw / 4) / NUM_STEPS)
        my_phaser.enable = 0

    def change_x_axis(self, state):
        global plot_dist
        plot_dist = state == Qt.Checked
        if plot_dist:
            self.fft_plot.setXRange(0, PLOT_FREQ * c / (4 * slope))
        else:
            self.fft_plot.setXRange(SIGNAL_FREQ - PLOT_FREQ, SIGNAL_FREQ + PLOT_FREQ)

    def update_display(self, data):
        global plot_dist, clutter_estimate
        label_style = {"color": "#FFF", "font-size": "14pt"}

        combined = data[0] + data[1]
        window = np.blackman(len(combined))
        spectrum = np.fft.fftshift(np.fft.fft(combined * window))
        if clutter_estimate is None:
            clutter_estimate = spectrum.copy()
        else:
            clutter_estimate[:] = CLUTTER_ALPHA * clutter_estimate + (1 - CLUTTER_ALPHA) * spectrum
        spectrum = spectrum - clutter_estimate
        spectrum = np.abs(spectrum)
        s_mag = np.abs(spectrum) / np.sum(window)
        s_mag = np.maximum(s_mag, 10 ** (-15))
        s_dbfs = 20 * np.log10(s_mag / (2 ** 11))

        if plot_dist:
            self.fft_curve.setData(dist, s_dbfs)
            self.fft_plot.setLabel("bottom", text="Distance", units="m", **label_style)
        else:
            self.fft_curve.setData(freq, s_dbfs)
            self.fft_plot.setLabel("bottom", text="Frequency", units="Hz", **label_style)

        self.img_array = np.roll(self.img_array, 1, axis=0)
        self.img_array[0] = s_dbfs
        self.imageitem.setLevels([self.low_slider.value(), self.high_slider.value()])
        self.imageitem.setImage(self.img_array, autoLevels=False)

        if self.frame_index == 1:
            self.fft_plot.enableAutoRange("xy", False)
        self.frame_index += 1

    def end_program(self):
        my_sdr.tx_destroy_buffer()
        self.close()


app = QApplication(sys.argv)
win = Window()

worker = RxWorker()
worker.data_ready.connect(win.update_display)
worker.start()

print("Full-band FMCW waterfall running. Close the window or press Quit to stop.")
ret = app.exec()

worker.requestInterruption()
worker.wait(2000)
try:
    my_sdr.tx_destroy_buffer()
except Exception:
    pass
sys.exit(ret)
