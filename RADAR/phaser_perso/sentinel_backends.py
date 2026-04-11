from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

import numpy as np

from sentinel_core import RadarConfig, segment_continuous_capture


class MissingRuntimeDependency(RuntimeError):
    pass


@dataclass
class CaptureFrame:
    chirp_matrix: np.ndarray
    config: RadarConfig
    raw_ch0: np.ndarray | None = None
    raw_ch1: np.ndarray | None = None
    valid_chirps: np.ndarray | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeRadarConfig:
    rpi_uri: str = "ip:phaser.local"
    sdr_uri: str = "ip:phaser.local:50901"
    sample_rate: float = 4e6
    center_freq: float = 2.1e9
    signal_freq: float = 100e3
    rx_gain: int = 60
    tx_gain: int = 0
    output_freq: float = 10.25e9
    chirp_bw: float = 500e6
    ramp_time_us: int = 500
    num_chirps: int = 64
    gain_taper: tuple[int, ...] = (127, 127, 127, 127, 127, 127, 127, 127)
    element_spacing: float = 0.014
    frame_guard_ms: float = 0.2
    tdd_sync_external: bool = True
    tdd_ext_capture: bool = True
    tdd_begin_offset_fraction: float = 0.1

    def continuous_core_config(self) -> RadarConfig:
        frame_length_ms = self.ramp_time_us / 1e3
        samples_per_chirp = int(round((self.ramp_time_us / 1e6) * self.sample_rate))
        return RadarConfig(
            sample_rate=self.sample_rate,
            signal_freq=self.signal_freq,
            output_freq=self.output_freq,
            num_chirps=self.num_chirps,
            chirp_bw=self.chirp_bw,
            ramp_time_s=self.ramp_time_us / 1e6,
            frame_length_ms=frame_length_ms,
            samples_per_chirp=samples_per_chirp,
        )

    def tdd_core_config(self, *, samples_per_chirp: int) -> RadarConfig:
        frame_length_ms = self.ramp_time_us / 1e3 + self.frame_guard_ms
        return RadarConfig(
            sample_rate=self.sample_rate,
            signal_freq=self.signal_freq,
            output_freq=self.output_freq,
            num_chirps=self.num_chirps,
            chirp_bw=self.chirp_bw,
            ramp_time_s=self.ramp_time_us / 1e6,
            frame_length_ms=frame_length_ms,
            samples_per_chirp=samples_per_chirp,
        )


def _import_adi():
    try:
        import adi  # type: ignore
    except ImportError as exc:  # pragma: no cover - hardware runtime only
        raise MissingRuntimeDependency(
            "The live backends require pyadi-iio in the radar virtual environment."
        ) from exc
    return adi


def _set_gpio(phaser, name: str, value: int) -> None:
    try:
        setattr(phaser._gpios, name, value)
    except AttributeError:
        setattr(phaser.gpios, name, value)


def _set_burst_gpio(phaser) -> None:
    for value in (0, 1, 0):
        _set_gpio(phaser, "gpio_burst", value)


class BaseBackend:
    def __init__(self, runtime: RuntimeRadarConfig):
        self.runtime = runtime
        self.my_sdr = None
        self.my_phaser = None
        self.core_config: RadarConfig | None = None

    @property
    def ready(self) -> bool:
        return self.my_sdr is not None and self.my_phaser is not None

    def _configure_common_hardware(self) -> None:
        adi = _import_adi()
        self.my_sdr = adi.ad9361(uri=self.runtime.sdr_uri)
        self.my_sdr._ctx.set_timeout(0)
        self.my_phaser = adi.CN0566(uri=self.runtime.rpi_uri, sdr=self.my_sdr)

        self.my_phaser.configure(device_mode="rx")
        self.my_phaser.element_spacing = self.runtime.element_spacing
        self.my_phaser.load_gain_cal()
        self.my_phaser.load_phase_cal()
        for channel in range(8):
            self.my_phaser.set_chan_phase(channel, 0)
        for channel, gain in enumerate(self.runtime.gain_taper):
            self.my_phaser.set_chan_gain(channel, gain, apply_cal=True)

        _set_gpio(self.my_phaser, "gpio_tx_sw", 0)
        _set_gpio(self.my_phaser, "gpio_vctrl_1", 1)
        _set_gpio(self.my_phaser, "gpio_vctrl_2", 1)

        self.my_sdr.sample_rate = int(self.runtime.sample_rate)
        self.my_sdr.rx_lo = int(self.runtime.center_freq)
        self.my_sdr.rx_enabled_channels = [0, 1]
        self.my_sdr.gain_control_mode_chan0 = "manual"
        self.my_sdr.gain_control_mode_chan1 = "manual"
        self.my_sdr.rx_hardwaregain_chan0 = int(self.runtime.rx_gain)
        self.my_sdr.rx_hardwaregain_chan1 = int(self.runtime.rx_gain)

        self.my_sdr.tx_lo = int(self.runtime.center_freq)
        self.my_sdr.tx_enabled_channels = [0, 1]
        self.my_sdr.tx_cyclic_buffer = True
        self.my_sdr.tx_hardwaregain_chan0 = -88
        self.my_sdr.tx_hardwaregain_chan1 = int(self.runtime.tx_gain)

    def _start_tx_tone(self) -> None:
        fs = int(self.my_sdr.sample_rate)
        length = int(2**18)
        ts = 1.0 / float(fs)
        tone = int(self.runtime.signal_freq)
        t = np.arange(0, length * ts, ts)
        i = np.cos(2 * np.pi * t * tone) * 2**14
        q = np.sin(2 * np.pi * t * tone) * 2**14
        iq = (0.9 * (i + 1j * q)).astype(np.complex64)
        try:
            self.my_sdr.tx_destroy_buffer()
        except Exception:
            pass
        self.my_sdr.tx([iq, iq])

    def set_steering_angle(self, angle_deg: float) -> None:
        if not self.ready:
            raise RuntimeError("Backend must be connected before steering.")
        phase_delta = (
            2.0
            * np.pi
            * self.runtime.output_freq
            * self.runtime.element_spacing
            * np.sin(np.radians(angle_deg))
            / 3e8
        )
        self.my_phaser.set_beam_phase_diff(np.degrees(phase_delta))

    def close(self) -> None:
        if self.my_sdr is None:
            return
        try:
            self.my_sdr.tx_destroy_buffer()
        except Exception:
            pass


class ContinuousSawtoothBackend(BaseBackend):
    name = "continuous"

    def connect(self) -> RadarConfig:
        self._configure_common_hardware()

        vco_freq = int(
            self.runtime.output_freq
            + self.runtime.signal_freq
            + self.runtime.center_freq
        )
        num_steps = int(self.runtime.ramp_time_us)
        self.my_phaser.frequency = int(vco_freq / 4)
        self.my_phaser.freq_dev_range = int(self.runtime.chirp_bw / 4)
        self.my_phaser.freq_dev_step = int((self.runtime.chirp_bw / 4) / num_steps)
        self.my_phaser.freq_dev_time = int(self.runtime.ramp_time_us)
        self.my_phaser.delay_word = 0
        self.my_phaser.delay_clk = "PFD"
        self.my_phaser.delay_start_en = 0
        self.my_phaser.ramp_delay_en = 0
        self.my_phaser.trig_delay_en = 0
        self.my_phaser.ramp_mode = "continuous_sawtooth"
        self.my_phaser.sing_ful_tri = 0
        self.my_phaser.tx_trig_en = 0
        self.my_phaser.enable = 0

        self.core_config = self.runtime.continuous_core_config()
        total_samples = (
            self.core_config.effective_samples_per_chirp * (self.runtime.num_chirps + 1)
        )
        self.my_sdr.rx_buffer_size = int(2 ** math.ceil(math.log2(total_samples)))

        self._start_tx_tone()
        return self.core_config

    def capture_chirp_matrix(self) -> CaptureFrame:
        if self.core_config is None:
            raise RuntimeError("Continuous backend has not been connected.")

        data = self.my_sdr.rx()
        raw_ch0 = np.asarray(data[0], dtype=np.complex64)
        raw_ch1 = np.asarray(data[1], dtype=np.complex64)
        chirps, valid = segment_continuous_capture(
            raw_ch0 + raw_ch1,
            num_chirps=self.runtime.num_chirps,
            samples_per_chirp=self.core_config.effective_samples_per_chirp,
            skip_chirps=1,
        )
        return CaptureFrame(
            chirp_matrix=chirps,
            config=self.core_config,
            raw_ch0=raw_ch0,
            raw_ch1=raw_ch1,
            valid_chirps=valid,
            diagnostics={
                "backend": self.name,
                "rx_buffer_size": int(self.my_sdr.rx_buffer_size),
            },
        )


class TDDBurstBackend(BaseBackend):
    name = "tdd"

    def _requires_gpio_burst(self) -> bool:
        # Internal TDD sync should let Pluto self-trigger the burst capture.
        return self.runtime.tdd_sync_external or self.runtime.tdd_ext_capture

    def connect(self) -> RadarConfig:
        adi = _import_adi()
        self._configure_common_hardware()

        vco_freq = int(
            self.runtime.output_freq
            + self.runtime.signal_freq
            + self.runtime.center_freq
        )
        num_steps = int(self.runtime.ramp_time_us)
        self.my_phaser.frequency = int(vco_freq / 4)
        self.my_phaser.freq_dev_range = int(self.runtime.chirp_bw / 4)
        self.my_phaser.freq_dev_step = int((self.runtime.chirp_bw / 4) / num_steps)
        self.my_phaser.freq_dev_time = int(self.runtime.ramp_time_us)
        self.my_phaser.delay_word = 4095
        self.my_phaser.delay_clk = "PFD"
        self.my_phaser.delay_start_en = 0
        self.my_phaser.ramp_delay_en = 0
        self.my_phaser.trig_delay_en = 0
        self.my_phaser.ramp_mode = "single_sawtooth_burst"
        self.my_phaser.sing_ful_tri = 0
        self.my_phaser.tx_trig_en = 1
        self.my_phaser.enable = 0

        self.sdr_pins = adi.one_bit_adc_dac(self.runtime.sdr_uri)
        self.sdr_pins.gpio_tdd_ext_sync = self.runtime.tdd_ext_capture
        self.sdr_pins.gpio_phaser_enable = True
        self.tdd = adi.tddn(self.runtime.sdr_uri)
        self.tdd.enable = False
        self.tdd.sync_external = self.runtime.tdd_sync_external
        self.tdd.startup_delay_ms = 0
        self.tdd.frame_length_ms = self.runtime.ramp_time_us / 1e3 + self.runtime.frame_guard_ms
        self.tdd.burst_count = self.runtime.num_chirps
        for channel in range(3):
            self.tdd.channel[channel].enable = True
            self.tdd.channel[channel].polarity = False
            self.tdd.channel[channel].on_raw = 0
            self.tdd.channel[channel].off_raw = 10
        self.tdd.enable = True

        ramp_time_us = int(self.my_phaser.freq_dev_time)
        ramp_time_s = ramp_time_us / 1e6
        begin_offset_time = self.runtime.tdd_begin_offset_fraction * ramp_time_s
        self.good_ramp_samples = int(
            (ramp_time_s - begin_offset_time) * self.runtime.sample_rate
        )
        self.num_samples_frame = int(
            self.tdd.frame_length_ms / 1000 * self.runtime.sample_rate
        )
        self.start_offset_samples = int(begin_offset_time * self.runtime.sample_rate)

        total_time_ms = self.tdd.frame_length_ms * self.runtime.num_chirps
        power = 12
        buffer_time_ms = 0.0
        while total_time_ms > buffer_time_ms:
            power += 1
            self.buffer_size = int(2**power)
            buffer_time_ms = self.buffer_size / self.runtime.sample_rate * 1000
            if power >= 22:
                break
        self.my_sdr.rx_buffer_size = self.buffer_size

        self.core_config = self.runtime.tdd_core_config(
            samples_per_chirp=self.good_ramp_samples
        )
        self._start_tx_tone()
        return self.core_config

    def capture_chirp_matrix(self) -> CaptureFrame:
        if self.core_config is None:
            raise RuntimeError("TDD backend has not been connected.")

        if self._requires_gpio_burst():
            _set_burst_gpio(self.my_phaser)
        data = self.my_sdr.rx()
        raw_ch0 = np.asarray(data[0], dtype=np.complex64)
        raw_ch1 = np.asarray(data[1], dtype=np.complex64)
        summed = raw_ch0 + raw_ch1

        chirps = np.zeros(
            (self.runtime.num_chirps, self.good_ramp_samples),
            dtype=np.complex64,
        )
        valid = np.zeros(self.runtime.num_chirps, dtype=bool)
        for chirp_idx in range(self.runtime.num_chirps):
            start = self.start_offset_samples + chirp_idx * self.num_samples_frame
            stop = start + self.good_ramp_samples
            if start >= summed.size:
                break
            chunk = summed[start:stop]
            chirps[chirp_idx, : chunk.size] = chunk
            valid[chirp_idx] = chunk.size == self.good_ramp_samples

        return CaptureFrame(
            chirp_matrix=chirps,
            config=self.core_config,
            raw_ch0=raw_ch0,
            raw_ch1=raw_ch1,
            valid_chirps=valid,
            diagnostics={
                "backend": self.name,
                "uses_gpio_burst": self._requires_gpio_burst(),
                "rx_buffer_size": int(self.my_sdr.rx_buffer_size),
                "frame_length_ms": float(self.tdd.frame_length_ms),
                "good_ramp_samples": int(self.good_ramp_samples),
                "num_samples_frame": int(self.num_samples_frame),
            },
        )


def create_backend(acquisition: str, runtime: RuntimeRadarConfig) -> BaseBackend:
    if acquisition == "tdd":
        return TDDBurstBackend(runtime)
    if acquisition == "continuous":
        return ContinuousSawtoothBackend(runtime)
    raise ValueError(f"Unsupported acquisition mode: {acquisition}")
