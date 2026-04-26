"""CW vitals DSP for SENTINEL Mode B.

Pipeline
--------
  CWProcessor.push(raw_iq)
      Mix raw IF IQ to baseband (phase-coherent across calls), decimate to
      ~100 Hz, push into a rolling ring buffer.

  CWProcessor.compute() -> VitalsResult | None
      From the ring buffer: unwrap phase, remove linear drift, bandpass for
      respiration (0.15–0.5 Hz) and heartbeat (0.8–2.5 Hz), convert to
      displacement (mm), estimate BPM from Welch PSD.  Returns None until
      enough history is available.

DSP reference: RADAR/matlab_phaser/heartbeat1.m
  Phase demodulation: mix(bb, exp(-j2π·fc·n/fs)) → decimate → unwrap → BPF.
  Scipy SOS form is used for filter stability at the very low cutoffs
  (0.15 Hz normalized = 0.003; ba-form Butterworth is numerically unstable).

Phase continuity across frames
-------------------------------
  mix_exp must be continuous across consecutive rx() calls or unwrap() will
  see a large phase jump and produce garbage.  We track the sample-offset
  modulo one IF cycle (cycle_len = round(fs / signal_freq) = 6 at the
  standard 600 kHz / 100 kHz settings) so the mix exponential has no seam
  at frame boundaries.

  Similarly we track the decimation phase (decim_pos) so the ~100 Hz sample
  stream is evenly spaced across frame boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfiltfilt, welch


SPEED_OF_LIGHT = 299_792_458.0


@dataclass
class VitalsResult:
    time_axis:    np.ndarray   # (N,) float32  seconds
    resp_disp_mm: np.ndarray   # (N,) float32  respiration displacement
    hr_disp_mm:   np.ndarray   # (N,) float32  heartbeat displacement
    resp_bpm:     float        # breaths / min
    hr_bpm:       float        # beats / min
    resp_conf:    float        # 0–1  SNR-based confidence
    hr_conf:      float        # 0–1
    psd_freq_hz:  np.ndarray   # (M,) float32  PSD frequency axis
    psd_db:       np.ndarray   # (M,) float32  PSD of detrended phase (dB/Hz)


class CWProcessor:
    """Stateful per-session CW vitals processor.

    Parameters
    ----------
    fs          Sample rate of raw IF IQ (Hz). Default 600 kHz.
    signal_freq IF tone (Hz). Default 100 kHz.
    rf_freq     Radiated RF center frequency (Hz). Default 10.25 GHz.
    history_sec Phase history window (s). Default 20 s.
    phase_fs    Target phase sample rate after decimation (Hz). Default 100 Hz.
    warmup_sec  Minimum phase-history duration before vitals are reported.
    """

    def __init__(
        self,
        fs:          float = 600e3,
        signal_freq: float = 100e3,
        rf_freq:     float = 10.25e9,
        history_sec: float = 20.0,
        phase_fs:    float = 100.0,
        warmup_sec:  float = 8.0,
    ) -> None:
        self.fs          = float(fs)
        self.signal_freq = float(signal_freq)
        self.rf_freq     = float(rf_freq)
        self.lambda_m    = SPEED_OF_LIGHT / rf_freq

        # Decimation
        self.decim    = max(1, round(fs / phase_fs))
        self.phase_fs = self.fs / self.decim   # actual phase rate

        # Ring buffer (complex baseband at phase_fs)
        self._hist_len = max(1, round(history_sec * self.phase_fs))
        self._ring     = np.zeros(self._hist_len, dtype=np.complex64)
        self._ptr      = 0    # write head
        self._n_pushed = 0    # total phase samples ever pushed

        # Phase continuity: track position within one IF period (mod cycle_len)
        # cycle_len = round(fs / signal_freq) = 6 at standard settings
        self._cycle_len  = max(1, round(self.fs / self.signal_freq))
        self._mix_offset = 0   # current IF-cycle position (0 .. cycle_len-1)
        self._decim_pos  = 0   # current decimation position (0 .. decim-1)

        # Band-pass filters — SOS form for numerical stability at low cutoffs
        nyq = self.phase_fs / 2.0
        lo_resp, hi_resp = 0.15 / nyq, 0.50 / nyq
        lo_hr,   hi_hr   = 0.80 / nyq, 2.50 / nyq
        lo_resp = np.clip(lo_resp, 1e-4, 0.99)
        hi_resp = np.clip(hi_resp, 1e-4, 0.99)
        lo_hr   = np.clip(lo_hr,   1e-4, 0.99)
        hi_hr   = np.clip(hi_hr,   1e-4, 0.99)
        self._sos_resp = butter(4, [lo_resp, hi_resp], btype="bandpass", output="sos")
        self._sos_hr   = butter(4, [lo_hr,   hi_hr],   btype="bandpass", output="sos")

        # Avoid early false BPM estimates from too little phase history.
        warmup = min(max(float(warmup_sec), 1.0), float(history_sec))
        self._min_samples = max(int(warmup * self.phase_fs), 256)

    # ------------------------------------------------------------------

    def push(self, raw_iq: np.ndarray) -> int:
        """Mix, decimate, and push into ring buffer.

        Returns the number of phase samples added this call.
        """
        n = len(raw_iq)
        if n == 0:
            return 0

        # 1. Mix to baseband — phase-continuous across calls
        idx = (np.arange(n, dtype=np.int64) + self._mix_offset) % self._cycle_len
        mix = np.exp(
            -1j * 2.0 * np.pi * self.signal_freq * idx / self.fs
        ).astype(np.complex64)
        bb = raw_iq.astype(np.complex64) * mix
        self._mix_offset = int((self._mix_offset + n) % self._cycle_len)

        # 2. Decimate — aligned across calls
        first = int((-self._decim_pos) % self.decim)
        self._decim_pos = int((self._decim_pos + n) % self.decim)
        if first >= n:
            return 0
        new_samp = bb[first::self.decim]

        # 3. Push into ring (circular)
        k = len(new_samp)
        end = self._ptr + k
        if end <= self._hist_len:
            self._ring[self._ptr:end] = new_samp
            self._ptr = end % self._hist_len
        else:
            # Wrap around
            part1 = self._hist_len - self._ptr
            self._ring[self._ptr:] = new_samp[:part1]
            part2 = k - part1
            self._ring[:part2] = new_samp[part1:]
            self._ptr = part2
        self._n_pushed += k
        return k

    def compute(self) -> VitalsResult | None:
        """Process ring buffer and return VitalsResult, or None if not ready."""
        n_avail = min(self._n_pushed, self._hist_len)
        if n_avail < self._min_samples:
            return None

        # Chronological ring view
        if self._n_pushed < self._hist_len:
            bb = self._ring[:self._n_pushed].copy()
        else:
            bb = np.roll(self._ring, -self._ptr).copy()

        # Phase demodulation (faithful to heartbeat1.m)
        bb_norm = bb / np.maximum(np.abs(bb), 1e-9)
        phi     = np.unwrap(np.angle(bb_norm))

        # Detrend — remove linear drift (slow panning motion, temperature)
        n   = len(phi)
        t   = np.arange(n, dtype=np.float64) / self.phase_fs
        coef = np.polyfit(t, phi, 1)
        phi_det = (phi - np.polyval(coef, t)).astype(np.float64)

        # Band-pass filter both bands
        resp_ph = sosfiltfilt(self._sos_resp, phi_det)
        hr_ph   = sosfiltfilt(self._sos_hr,   phi_det)

        # Phase → displacement (mm)  round-trip: d = φ × λ / (4π)
        scale_mm     = self.lambda_m / (4.0 * np.pi) * 1e3
        resp_disp_mm = (resp_ph * scale_mm).astype(np.float32)
        hr_disp_mm   = (hr_ph   * scale_mm).astype(np.float32)

        # BPM estimates
        resp_bpm, resp_conf = _bpm_from_welch(resp_ph, self.phase_fs, 0.15, 0.50)
        hr_bpm,   hr_conf   = _bpm_from_welch(hr_ph,   self.phase_fs, 0.80, 2.50)

        # Full-band PSD (0–4 Hz for display)
        nperseg = min(len(phi_det), 512)
        f_psd, pxx = welch(phi_det, fs=self.phase_fs, nperseg=nperseg,
                           noverlap=nperseg // 2)
        pxx_db  = (10.0 * np.log10(np.maximum(pxx, 1e-12))).astype(np.float32)
        mask_4hz = f_psd <= 4.0
        f_psd   = f_psd[mask_4hz].astype(np.float32)
        pxx_db  = pxx_db[mask_4hz]

        return VitalsResult(
            time_axis    = t.astype(np.float32),
            resp_disp_mm = resp_disp_mm,
            hr_disp_mm   = hr_disp_mm,
            resp_bpm     = float(resp_bpm),
            hr_bpm       = float(hr_bpm),
            resp_conf    = float(resp_conf),
            hr_conf      = float(hr_conf),
            psd_freq_hz  = f_psd,
            psd_db       = pxx_db,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bpm_from_welch(
    signal: np.ndarray, fs: float, f_lo: float, f_hi: float
) -> tuple[float, float]:
    """Dominant rate (BPM) and confidence (0–1) from Welch PSD peak.

    Uses the full signal as a single segment so the frequency resolution is
    1/duration Hz — critical for respiration where 1 BPM = 0.017 Hz and
    nperseg=512 at 100 Hz gives only 0.195 Hz bins (11.7 BPM per bin).
    """
    n = len(signal)
    if n < 64:
        return 0.0, 0.0
    # Full-length periodogram for maximum frequency resolution.
    # For 20 s @ 100 Hz: f_res = 0.05 Hz = 3 BPM — sufficient for both bands.
    nperseg = n
    f, pxx  = welch(signal, fs=fs, nperseg=nperseg, noverlap=0)
    mask    = (f >= f_lo) & (f <= f_hi)
    if not mask.any():
        return 0.0, 0.0
    pxx_b    = pxx[mask]
    f_b      = f[mask]
    peak_idx = int(np.argmax(pxx_b))
    snr      = float(pxx_b[peak_idx]) / (float(np.mean(pxx_b)) + 1e-15)
    conf     = float(np.clip((snr - 1.0) / 9.0, 0.0, 1.0))
    bpm      = float(f_b[peak_idx] * 60.0)
    return bpm, conf
