"""Muscle-proxy DSP for SENTINEL Mode C.

Pipeline
--------
  MyoProcessor.push(raw_iq)
      Mix raw IF IQ to baseband, extract amplitude envelope, decimate to
      ~2 kHz, push into rolling ring buffer.

  MyoProcessor.compute() -> MyoResult | None
      FFT of the most recent window of the amplitude ring buffer.
      Returns spectral energy and centroid in the EMG proxy band [10–500 Hz],
      plus a normalised myo-index (0–1) relative to a calibration baseline.
      Returns None until enough history is available.
      The envelope mean is removed before the FFT so DC carrier strength does
      not leak into the proxy band.

Physics
-------
  Muscle contraction redistributes intramuscular water, shifting tissue
  dielectric permittivity.  This modulates the amplitude of the reflected CW
  signal at motor-unit firing rates (~20–500 Hz).  Phase also shifts but is
  dominated at these frequencies by bulk motion; amplitude is more specific
  to muscle activity at close range (<0.5 m).

Myo-index
---------
  index = clip( (E - E_base) / (3·σ_base + ε), 0, 1 )

  E       = spectral energy in [band_lo, band_hi] for the current window.
  E_base  = mean energy during calibration (first baseline_sec seconds).
  σ_base  = std  energy during calibration.

  index > threshold → "ACTIVE"

Phase continuity
----------------
  Mix exponential tracks offset mod cycle_len exactly as in cw_dsp.py so
  push() calls are composable across frames.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class MyoResult:
    myo_index:      float        # 0–1  current activation level
    activation:     bool         # True if myo_index > threshold
    cur_energy_db:  float        # dB  spectral energy in EMG band (current window)
    cur_centroid_hz: float       # Hz  spectral centroid in EMG band (current window)
    spec_freq_hz:   np.ndarray   # (M,) float32  frequency axis for display PSD
    spec_db:        np.ndarray   # (M,) float32  current window PSD in dB
    calibrated:     bool         # True once baseline is established
    cal_progress:   float        # 0–1  fraction of calibration window filled


class MyoProcessor:
    """Stateful per-session muscle-proxy processor.

    Parameters
    ----------
    fs              ADC sample rate (Hz). Default 600 kHz.
    signal_freq     IF tone (Hz). Default 100 kHz.
    phase_fs        Target envelope sample rate after decimation (Hz). Default 2000 Hz.
    history_sec     Ring buffer length (s). Default 10 s.
    baseline_sec    Calibration window (s). Default 3 s.
    win_sec         Analysis window length (s). Default 0.25 s.
    band_lo         EMG proxy band low  cutoff (Hz). Default 10 Hz.
    band_hi         EMG proxy band high cutoff (Hz). Default 500 Hz.
    threshold       Myo-index activation threshold. Default 0.3.
    """

    def __init__(
        self,
        fs:           float = 600e3,
        signal_freq:  float = 100e3,
        phase_fs:     float = 2000.0,
        history_sec:  float = 10.0,
        baseline_sec: float = 3.0,
        win_sec:      float = 0.25,
        band_lo:      float = 10.0,
        band_hi:      float = 500.0,
        threshold:    float = 0.3,
    ) -> None:
        self.fs          = float(fs)
        self.signal_freq = float(signal_freq)
        self.threshold   = float(threshold)
        self.band_lo     = float(band_lo)
        self.band_hi     = float(band_hi)

        # Decimation
        self.decim    = max(1, round(fs / phase_fs))
        self.phase_fs = self.fs / self.decim   # actual rate

        # Ring buffer — amplitude envelope at phase_fs
        self._hist_len = max(1, round(history_sec * self.phase_fs))
        self._ring     = np.zeros(self._hist_len, dtype=np.float32)
        self._ptr      = 0
        self._n_pushed = 0   # total envelope samples ever pushed

        # Phase-continuous mix tracking (same logic as cw_dsp.py)
        self._cycle_len  = max(1, round(self.fs / self.signal_freq))
        self._mix_offset = 0
        self._decim_pos  = 0

        # Analysis window
        self._win_len = max(64, round(win_sec * self.phase_fs))
        self._win_fn  = np.hanning(self._win_len).astype(np.float32)

        # Calibration
        self._cal_len            = max(1, round(baseline_sec * self.phase_fs))
        self._cal_energy_buf:    list[float] = []
        self._cal_start_n_pushed = 0
        self._cal_energy_mean    = 0.0
        self._cal_energy_std     = 1.0
        self._calibrated         = False

        self._min_samples = self._win_len

    # ------------------------------------------------------------------

    def reset_calibration(self) -> None:
        """Discard baseline and restart calibration from the next push."""
        self._cal_energy_buf.clear()
        self._calibrated      = False
        self._cal_start_n_pushed = self._n_pushed
        self._cal_energy_mean = 0.0
        self._cal_energy_std  = 1.0

    def push(self, raw_iq: np.ndarray) -> int:
        """Mix to baseband, extract amplitude envelope, decimate, push to ring.

        Returns the number of envelope samples added this call.
        """
        n = len(raw_iq)
        if n == 0:
            return 0

        # Mix to baseband — phase-continuous across calls
        idx = (np.arange(n, dtype=np.int64) + self._mix_offset) % self._cycle_len
        mix = np.exp(
            -1j * 2.0 * np.pi * self.signal_freq * idx / self.fs
        ).astype(np.complex64)
        bb = raw_iq.astype(np.complex64) * mix
        self._mix_offset = int((self._mix_offset + n) % self._cycle_len)

        # Amplitude envelope
        amp = np.abs(bb).astype(np.float32)

        # Decimate — aligned across calls
        first = int((-self._decim_pos) % self.decim)
        self._decim_pos = int((self._decim_pos + n) % self.decim)
        if first >= n:
            return 0
        new_samp = amp[first::self.decim]

        # Push into ring (circular)
        k = len(new_samp)
        end = self._ptr + k
        if end <= self._hist_len:
            self._ring[self._ptr:end] = new_samp
            self._ptr = end % self._hist_len
        else:
            part1 = self._hist_len - self._ptr
            self._ring[self._ptr:] = new_samp[:part1]
            part2 = k - part1
            self._ring[:part2] = new_samp[part1:]
            self._ptr = part2
        self._n_pushed += k
        return k

    def compute(self) -> MyoResult | None:
        """Compute current spectral features and myo-index. Returns None if not ready."""
        n_avail = min(self._n_pushed, self._hist_len)
        if n_avail < self._min_samples:
            return None

        # Ordered ring view
        if self._n_pushed < self._hist_len:
            buf = self._ring[:self._n_pushed].copy()
        else:
            buf = np.roll(self._ring, -self._ptr).copy()

        n = len(buf)

        # Analysis window: latest win_len samples
        wlen = min(self._win_len, n)
        seg_raw = buf[-wlen:]
        seg  = (seg_raw - float(np.mean(seg_raw))) * self._win_fn[:wlen]

        # FFT → power spectrum
        sp   = np.fft.rfft(seg.astype(np.float64))
        pxx  = (np.abs(sp) ** 2) / (float(np.sum(self._win_fn[:wlen])) ** 2 + 1e-30)
        freq = np.fft.rfftfreq(wlen, 1.0 / self.phase_fs)

        # EMG proxy band features
        mask = (freq >= self.band_lo) & (freq <= self.band_hi)
        if mask.any():
            pxx_b        = pxx[mask]
            f_b          = freq[mask]
            cur_energy   = float(np.sum(pxx_b))
            denom        = cur_energy + 1e-30
            cur_centroid = float(np.sum(f_b * pxx_b) / denom)
        else:
            cur_energy   = 0.0
            cur_centroid = 0.0

        # Feed calibration baseline
        cal_progress = 0.0
        if not self._calibrated:
            self._cal_energy_buf.append(cur_energy)
            cal_elapsed = max(0, self._n_pushed - self._cal_start_n_pushed)
            cal_progress = min(1.0, cal_elapsed / self._cal_len)
            # Require both elapsed baseline time and enough spectral snapshots.
            min_wins = max(6, round(self._cal_len / self._win_len))
            if cal_elapsed >= self._cal_len and len(self._cal_energy_buf) >= min_wins:
                arr = np.array(self._cal_energy_buf, dtype=np.float64)
                self._cal_energy_mean = float(np.mean(arr))
                self._cal_energy_std  = max(float(np.std(arr)), 1e-9)
                self._calibrated = True

        # Myo-index
        myo_index = float(np.clip(
            (cur_energy - self._cal_energy_mean) / (3.0 * self._cal_energy_std + 1e-15),
            0.0, 1.0,
        ))
        activation = bool(myo_index > self.threshold)

        # Display PSD (0–600 Hz)
        mask_disp = freq <= 600.0
        spec_db   = (10.0 * np.log10(np.maximum(pxx[mask_disp], 1e-30))).astype(np.float32)

        return MyoResult(
            myo_index       = myo_index,
            activation      = activation,
            cur_energy_db   = float(10.0 * np.log10(cur_energy + 1e-30)),
            cur_centroid_hz = cur_centroid,
            spec_freq_hz    = freq[mask_disp].astype(np.float32),
            spec_db         = spec_db,
            calibrated      = self._calibrated,
            cal_progress    = cal_progress,
        )
