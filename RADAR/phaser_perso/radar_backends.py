"""FMCW acquisition backends for CN0566 (Phaser) + AD9361 (Pluto).

Two backends share one hardware bring-up sequence and one chirp-matrix
contract.  Everything else is backend-specific.

Backend contract
----------------
  connect() -> RadarConfig
      Bring up hardware, program PLL/TDD, size buffers.
      Returns the RadarConfig describing what was actually programmed.

  capture() -> CaptureResult
      Fire one acquisition cycle.
      Returns CaptureResult with:
          chirp_matrix : (num_chirps, spc) complex64   <- the DSP input
          cfg          : RadarConfig
          ch0, ch1     : raw channel arrays (optional)
          valid        : (num_chirps,) bool

  close()
      Destroy TX buffer and release resources.

TDDBurstBackend   -- stays as close as possible to reference/Range_Doppler_Plot.py
ContinuousSawtoothBackend -- derived from Range_Doppler_NoTDD.py
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from radar_dsp import RadarConfig, segment_buffer


# ---------------------------------------------------------------------------
# Capture result
# ---------------------------------------------------------------------------

@dataclass
class CaptureResult:
    chirp_matrix: np.ndarray          # (num_chirps, spc) complex64
    cfg: RadarConfig
    ch0: np.ndarray | None = None     # raw Pluto RX0 buffer
    ch1: np.ndarray | None = None     # raw Pluto RX1 buffer
    valid: np.ndarray | None = None   # (num_chirps,) bool
    info: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runtime parameters (hardware addresses + tuning)
# ---------------------------------------------------------------------------

@dataclass
class HardwareParams:
    """All tunable parameters for a live session.

    Defaults match the 500 MHz / 500 us / 64-chirp entry in the tuning ladder.
    """
    rpi_uri: str = "ip:phaser.local"
    sdr_uri: str = "ip:phaser.local:50901"
    tdd_uri: str | None = None

    sample_rate: float = 4e6
    center_freq: float = 2.1e9
    signal_freq: float = 100e3
    rx_gain: int = 60       # dB, -3 to 70
    tx_gain: int = 0        # dB, 0 to -88 (0 = max power)
    output_freq: float = 10.25e9

    chirp_bw: float = 500e6
    ramp_time_us: int = 500
    num_chirps: int = 64

    gain_taper: tuple[int, ...] = (127,) * 8
    element_spacing: float = 0.014     # m (half-wavelength at ~10.7 GHz)

    # TDD-only
    frame_guard_ms: float = 0.2        # added to ramp to form PRI
    tdd_sync_external: bool = True
    tdd_ext_capture: bool = True
    tdd_begin_offset_frac: float = 0.1 # fraction of ramp to skip at start
    tdd_arm_delay_s: float = 0.5       # wait after starting rx() before gpio_burst


# ---------------------------------------------------------------------------
# Shared hardware bring-up
# ---------------------------------------------------------------------------

def _import_adi():
    try:
        import adi
    except ImportError as exc:
        raise RuntimeError(
            "pyadi-iio is required for live capture. "
            "Install it in the radar venv."
        ) from exc
    return adi


def _gpio(phaser, name: str, value) -> None:
    """Set a GPIO; tries _gpios first (newer firmware), falls back to gpios."""
    for attr in ("_gpios", "gpios"):
        obj = getattr(phaser, attr, None)
        if obj is not None:
            setattr(obj, name, value)
            return
    raise AttributeError(f"Cannot set {name} on phaser object")


class _BaseBackend:
    def __init__(self, hw: HardwareParams) -> None:
        self.hw = hw
        self._sdr = None
        self._phaser = None

    def _bring_up_hardware(self) -> None:
        adi = _import_adi()

        self._sdr = adi.ad9361(uri=self.hw.sdr_uri)
        # A finite timeout is safer than an infinite block over the forwarded
        # WSL -> Pi -> Pluto path.  When transport stalls we want a recoverable
        # exception and cleanup, not a frozen GUI that keeps the Pluto busy.
        self._sdr._ctx.set_timeout(5000)
        self._phaser = adi.CN0566(uri=self.hw.rpi_uri, sdr=self._sdr)

        # Phaser array
        self._phaser.configure(device_mode="rx")
        self._phaser.element_spacing = self.hw.element_spacing
        self._phaser.load_gain_cal()
        self._phaser.load_phase_cal()
        for ch in range(8):
            self._phaser.set_chan_phase(ch, 0)
        for ch, g in enumerate(self.hw.gain_taper):
            self._phaser.set_chan_gain(ch, g, apply_cal=True)

        _gpio(self._phaser, "gpio_tx_sw", 0)      # TX_OUT_2
        _gpio(self._phaser, "gpio_vctrl_1", 1)    # onboard PLL/LO
        _gpio(self._phaser, "gpio_vctrl_2", 1)    # enable TX path

        # SDR RX
        self._sdr.sample_rate = int(self.hw.sample_rate)
        self._sdr.rx_lo = int(self.hw.center_freq)
        self._sdr.rx_enabled_channels = [0, 1]
        self._sdr.gain_control_mode_chan0 = "manual"
        self._sdr.gain_control_mode_chan1 = "manual"
        self._sdr.rx_hardwaregain_chan0 = int(self.hw.rx_gain)
        self._sdr.rx_hardwaregain_chan1 = int(self.hw.rx_gain)

        # SDR TX
        self._sdr.tx_lo = int(self.hw.center_freq)
        self._sdr.tx_enabled_channels = [0, 1]
        self._sdr.tx_cyclic_buffer = True
        self._sdr.tx_hardwaregain_chan0 = -88                  # TX0 off
        self._sdr.tx_hardwaregain_chan1 = int(self.hw.tx_gain) # TX1 active

    def _program_pll(self, ramp_mode: str, delay_word: int, tx_trig_en: int) -> None:
        vco_freq = int(
            self.hw.output_freq + self.hw.signal_freq + self.hw.center_freq
        )
        bw = self.hw.chirp_bw
        num_steps = int(self.hw.ramp_time_us)   # 1 step per us is stable
        self._phaser.frequency = int(vco_freq / 4)
        self._phaser.freq_dev_range = int(bw / 4)
        self._phaser.freq_dev_step = int((bw / 4) / num_steps)
        self._phaser.freq_dev_time = int(self.hw.ramp_time_us)
        self._phaser.delay_word = delay_word
        self._phaser.delay_clk = "PFD"
        self._phaser.delay_start_en = 0
        self._phaser.ramp_delay_en = 0
        self._phaser.trig_delay_en = 0
        self._phaser.ramp_mode = ramp_mode
        self._phaser.sing_ful_tri = 0
        self._phaser.tx_trig_en = tx_trig_en
        self._phaser.enable = 0   # write last to latch all registers

    def _start_tx_tone(self) -> None:
        """Transmit a cyclic IF sinewave (matches Range_Doppler_Plot.py)."""
        N = int(2 ** 18)
        fs = int(self.hw.sample_rate)
        fc = int(self.hw.signal_freq)
        ts = 1.0 / float(fs)
        t = np.arange(0, N * ts, ts)
        i = np.cos(2 * np.pi * t * fc) * 2 ** 14
        q = np.sin(2 * np.pi * t * fc) * 2 ** 14
        iq = (0.9 * (i + 1j * q)).astype(np.complex64)
        try:
            self._sdr.tx_destroy_buffer()
        except Exception:
            pass
        try:
            self._sdr.tx([iq, iq])
        except OSError as exc:
            if exc.errno == 16:
                raise RuntimeError(
                    "Pluto TX buffer is busy. Another radar process is still "
                    "holding the device, or the previous run did not exit "
                    "cleanly. Stop the older process or restart iiod."
                ) from exc
            raise

    def set_steering_angle(self, angle_deg: float) -> None:
        if self._phaser is None:
            raise RuntimeError("Not connected")
        phase_delta = (
            2.0 * np.pi * self.hw.output_freq * self.hw.element_spacing
            * np.sin(np.radians(angle_deg)) / 3e8
        )
        self._phaser.set_beam_phase_diff(np.degrees(phase_delta))

    def close(self) -> None:
        if self._sdr is not None:
            try:
                self._sdr.tx_destroy_buffer()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# TDD burst backend  (Range_Doppler_Plot.py reference path)
# ---------------------------------------------------------------------------

class TDDBurstBackend(_BaseBackend):
    """Single-sawtooth-burst mode with TDD engine synchronisation.

    Follows Range_Doppler_Plot.py as closely as allowed:
    - ADF4159: single_sawtooth_burst, delay_word=4095, tx_trig_en=1
    - TDD: gpio_tdd_ext_sync=True, frame_length_ms = ramp + 0.2 ms guard
    - Buffer: smallest power-of-2 that covers all chirps
    - Capture: gpio_burst pulse, then rx()
    - Segmentation: start_offset_samples + burst * N_frame, good_ramp_samples

    Transport limitation
    --------------------
    TDD requires the Pluto to fill a large DMA burst in one rx() call.
    Over a forwarded IIO context (phaser.local:50901), iiod drops the TCP
    connection under that load -> TimeoutError / ETIMEDOUT.

    This backend works reliably when the SDR URI is a direct Pluto connection
    (ip:192.168.2.1) or when running the script directly on the Pi via SSH.

    If you get ETIMEDOUT on capture(), switch to ContinuousSawtoothBackend:
        python radar_mode_a.py --acq continuous
    """

    name = "tdd"

    def __init__(self, hw: HardwareParams) -> None:
        super().__init__(hw)
        self._cfg: RadarConfig | None = None
        self._tdd = None
        self._good_ramp_samples: int = 0
        self._n_frame: int = 0
        self._start_offset_samples: int = 0

    def connect(self) -> RadarConfig:
        adi = _import_adi()
        self._bring_up_hardware()
        self._program_pll("single_sawtooth_burst", delay_word=4095, tx_trig_en=1)

        # TDD engine setup -- matches Range_Doppler_Plot.py lines 141-165
        tdd_uri = self.hw.tdd_uri or self.hw.sdr_uri
        sdr_pins = adi.one_bit_adc_dac(tdd_uri)
        sdr_pins.gpio_tdd_ext_sync = self.hw.tdd_ext_capture
        sdr_pins.gpio_phaser_enable = True

        tdd = adi.tddn(tdd_uri)
        tdd.enable = False
        tdd.sync_external = self.hw.tdd_sync_external
        tdd.startup_delay_ms = 0
        pri_ms = self.hw.ramp_time_us / 1e3 + self.hw.frame_guard_ms
        tdd.frame_length_ms = pri_ms
        tdd.burst_count = self.hw.num_chirps
        for ch in range(3):
            tdd.channel[ch].enable = True
            tdd.channel[ch].polarity = False
            tdd.channel[ch].on_raw = 0
            tdd.channel[ch].off_raw = 10
        # External-sync TDD can stay enabled because it waits for gpio_burst.
        # Internal TDD starts as soon as it is enabled, so arm it per capture
        # after rx() is already blocking on the DMA buffer.
        tdd.enable = bool(self.hw.tdd_sync_external)
        self._tdd = tdd

        # Ramp timing (read back from hardware after programming)
        ramp_time_us = int(self._phaser.freq_dev_time)
        ramp_time_s = ramp_time_us / 1e6
        begin_offset_s = self.hw.tdd_begin_offset_frac * ramp_time_s
        self._good_ramp_samples = int(
            (ramp_time_s - begin_offset_s) * self.hw.sample_rate
        )
        self._n_frame = int(tdd.frame_length_ms / 1000 * self.hw.sample_rate)
        self._start_offset_samples = int(begin_offset_s * self.hw.sample_rate)

        # Buffer size: smallest 2^n that covers all chirps
        total_ms = tdd.frame_length_ms * self.hw.num_chirps
        power = 12
        buf_ms = 0.0
        while total_ms > buf_ms:
            power += 1
            buf_size = int(2 ** power)
            buf_ms = buf_size / self.hw.sample_rate * 1000
            if power >= 22:
                break
        self._sdr.rx_buffer_size = buf_size

        self._cfg = RadarConfig(
            sample_rate=self.hw.sample_rate,
            signal_freq=self.hw.signal_freq,
            output_freq=self.hw.output_freq,
            num_chirps=self.hw.num_chirps,
            chirp_bw=self.hw.chirp_bw,
            ramp_time_s=ramp_time_s,
            frame_length_ms=float(tdd.frame_length_ms),
            samples_per_chirp=self._good_ramp_samples,
        )
        self._start_tx_tone()
        return self._cfg

    def capture(self) -> CaptureResult:
        if self._cfg is None:
            raise RuntimeError("Call connect() first")

        import threading
        import time

        result: dict = {}
        exc_box: list = []

        def _do_rx() -> None:
            try:
                result["data"] = self._sdr.rx()
            except Exception as exc:  # noqa: BLE001
                exc_box.append(exc)

        # ARM the DMA first -- rx() blocks on the Pluto waiting for the trigger.
        # Firing gpio_burst before rx() is called races against DMA setup and
        # causes ETIMEDOUT.  Give the thread 50 ms to reach the Pluto iiod and
        # arm the DMA before the GPIO pulse fires.
        t = threading.Thread(target=_do_rx, daemon=True)
        t.start()
        time.sleep(max(0.0, self.hw.tdd_arm_delay_s))

        # Now start the burst.  External sync is triggered by Phaser GPIO;
        # internal sync is triggered by enabling the Pluto TDD engine.
        if self.hw.tdd_sync_external:
            for v in (0, 1, 0):
                _gpio(self._phaser, "gpio_burst", v)
        else:
            self._tdd.enable = False
            self._tdd.enable = True

        t.join(timeout=10.0)
        if not self.hw.tdd_sync_external:
            try:
                self._tdd.enable = False
            except Exception:
                pass

        if exc_box:
            exc = exc_box[0]
            if isinstance(exc, OSError) and exc.errno == 110:  # ETIMEDOUT
                raise RuntimeError(
                    "TDD rx() timed out (ETIMEDOUT) even after DMA-first fix.\n"
                    "\n"
                    f"The {self.hw.tdd_arm_delay_s:.3f} s DMA arm window may not be enough.  Try:\n"
                    "  1. Increase --tdd-arm-delay-s.\n"
                    "  2. Reduce buffer size: lower --num-chirps or --ramp-time-us.\n"
                    "  3. Use continuous backend: python radar_mode_a.py --acq continuous\n"
                ) from exc
            raise exc

        if "data" not in result:
            raise RuntimeError("TDD capture thread ended without returning data (timeout).")

        data = result["data"]
        ch0 = np.asarray(data[0], dtype=np.complex64)
        ch1 = np.asarray(data[1], dtype=np.complex64)
        summed = ch0 + ch1

        # Slice into chirp matrix -- same loop as Range_Doppler_Plot.py lines 261-267
        spc = self._good_ramp_samples
        chirps = np.zeros((self.hw.num_chirps, spc), dtype=np.complex64)
        valid = np.zeros(self.hw.num_chirps, dtype=bool)
        for k in range(self.hw.num_chirps):
            s = self._start_offset_samples + k * self._n_frame
            e = s + spc
            if s >= summed.size:
                break
            chunk = summed[s:e]
            chirps[k, : chunk.size] = chunk
            valid[k] = chunk.size == spc

        return CaptureResult(
            chirp_matrix=chirps,
            cfg=self._cfg,
            ch0=ch0,
            ch1=ch1,
            valid=valid,
            info={
                "backend": self.name,
                "rx_buffer_size": int(self._sdr.rx_buffer_size),
                "good_ramp_samples": spc,
                "n_frame": self._n_frame,
                "frame_length_ms": float(self._tdd.frame_length_ms),
            },
        )


# ---------------------------------------------------------------------------
# Continuous sawtooth backend  (Range_Doppler_NoTDD.py reference path)
# ---------------------------------------------------------------------------

class ContinuousSawtoothBackend(_BaseBackend):
    """Free-running continuous sawtooth, no TDD burst sync.

    Follows Range_Doppler_NoTDD.py:
    - ADF4159: continuous_sawtooth, delay_word=0, tx_trig_en=0
    - Buffer: smallest 2^n that covers (num_chirps + 1) frames
    - Capture: one rx() call, skip first chirp (may start mid-period),
               segment remaining num_chirps frames
    """

    name = "continuous"

    def __init__(self, hw: HardwareParams) -> None:
        super().__init__(hw)
        self._cfg: RadarConfig | None = None
        self._n_frame: int = 0

    def connect(self) -> RadarConfig:
        self._bring_up_hardware()
        self._program_pll("continuous_sawtooth", delay_word=0, tx_trig_en=0)

        # Read back actual ramp time
        ramp_time_us = int(self._phaser.freq_dev_time)
        ramp_time_s = ramp_time_us / 1e6
        self._n_frame = int(ramp_time_s * self.hw.sample_rate)

        # Buffer: (num_chirps + 1) * N_frame, rounded up to next power of 2
        total = (self.hw.num_chirps + 1) * self._n_frame
        buf_size = int(2 ** math.ceil(math.log2(total)))
        self._sdr.rx_buffer_size = buf_size

        self._cfg = RadarConfig(
            sample_rate=self.hw.sample_rate,
            signal_freq=self.hw.signal_freq,
            output_freq=self.hw.output_freq,
            num_chirps=self.hw.num_chirps,
            chirp_bw=self.hw.chirp_bw,
            ramp_time_s=ramp_time_s,
            frame_length_ms=ramp_time_s * 1e3,
            samples_per_chirp=self._n_frame,
        )
        self._start_tx_tone()
        return self._cfg

    def capture(self) -> CaptureResult:
        if self._cfg is None:
            raise RuntimeError("Call connect() first")
        try:
            data = self._sdr.rx()
        except OSError as exc:
            if exc.errno == 110:
                raise RuntimeError(
                    "Continuous rx() timed out over the forwarded IIO path. "
                    "The capture loop stalled, so the GUI looked frozen. "
                    "Restart the previous radar process or restart iiod, then retry."
                ) from exc
            raise
        ch0 = np.asarray(data[0], dtype=np.complex64)
        ch1 = np.asarray(data[1], dtype=np.complex64)

        chirps, valid = segment_buffer(
            ch0 + ch1,
            num_chirps=self.hw.num_chirps,
            samples_per_chirp=self._n_frame,
            skip_chirps=1,
        )

        return CaptureResult(
            chirp_matrix=chirps,
            cfg=self._cfg,
            ch0=ch0,
            ch1=ch1,
            valid=valid,
            info={
                "backend": self.name,
                "rx_buffer_size": int(self._sdr.rx_buffer_size),
                "n_frame": self._n_frame,
                "tdd_uri": self.hw.tdd_uri or self.hw.sdr_uri,
            },
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_backend(acq: str, hw: HardwareParams) -> _BaseBackend:
    """Return the correct backend for the given acquisition mode string."""
    if acq == "tdd":
        return TDDBurstBackend(hw)
    if acq == "continuous":
        return ContinuousSawtoothBackend(hw)
    raise ValueError(f"Unknown acquisition mode '{acq}'. Choose 'tdd' or 'continuous'.")
