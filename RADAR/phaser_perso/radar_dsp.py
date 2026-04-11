"""Shared FMCW DSP stack -- backend-neutral.

Contract
--------
Every function that processes radar data accepts a chirp matrix with shape
  (num_chirps, samples_per_chirp), dtype complex64
and is independent of how that matrix was acquired (TDD burst or continuous
sawtooth).

Config schema is kept compatible with Range_Doppler_Processing.py:
  [sample_rate, signal_freq, output_freq, num_chirps, chirp_BW, ramp_time_s, frame_length_ms]
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

_C = 3e8


# ---------------------------------------------------------------------------
# Radar configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RadarConfig:
    sample_rate: float       # Hz
    signal_freq: float       # Hz   (IF tone frequency)
    output_freq: float       # Hz   (RF output, used for range/velocity axes)
    num_chirps: int
    chirp_bw: float          # Hz
    ramp_time_s: float       # s    (actual ramp duration, excluding guard)
    frame_length_ms: float   # ms   (PRI = ramp + guard, or just ramp for continuous)
    samples_per_chirp: int | None = None  # override; derived from frame_length_ms if None

    # -- derived properties ------------------------------------------------

    @property
    def n_frame(self) -> int:
        """Samples per chirp period (PRI)."""
        if self.samples_per_chirp is not None:
            return int(self.samples_per_chirp)
        return int(round((self.frame_length_ms / 1e3) * self.sample_rate))

    @property
    def pri_s(self) -> float:
        return self.frame_length_ms / 1e3

    @property
    def prf(self) -> float:
        return 1.0 / self.pri_s

    @property
    def wavelength(self) -> float:
        return _C / self.output_freq

    @property
    def slope(self) -> float:
        return self.chirp_bw / self.ramp_time_s

    @property
    def range_res_m(self) -> float:
        return _C / (2.0 * self.chirp_bw)

    @property
    def vel_res_m_s(self) -> float:
        return self.wavelength / (2.0 * self.num_chirps * self.pri_s)

    @property
    def max_vel_m_s(self) -> float:
        return (self.prf / 2.0) * self.wavelength / 2.0

    def range_axis(self) -> np.ndarray:
        """Range in metres for each range-FFT bin."""
        freq = np.linspace(-self.sample_rate / 2, self.sample_rate / 2, self.n_frame)
        return (freq - self.signal_freq) * _C / (2.0 * self.slope)

    def velocity_axis(self) -> np.ndarray:
        """Velocity in m/s for each Doppler-FFT bin."""
        return np.linspace(-self.max_vel_m_s, self.max_vel_m_s, self.num_chirps)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["n_frame"] = self.n_frame
        d["range_res_m"] = self.range_res_m
        d["vel_res_m_s"] = self.vel_res_m_s
        d["max_vel_m_s"] = self.max_vel_m_s
        return d

    def to_adi_array(self) -> np.ndarray:
        """7-element array compatible with Range_Doppler_Processing.py."""
        return np.array([
            self.sample_rate,
            self.signal_freq,
            self.output_freq,
            self.num_chirps,
            self.chirp_bw,
            self.ramp_time_s,
            self.frame_length_ms,
        ], dtype=float)

    @staticmethod
    def from_adi_array(arr: np.ndarray, *, samples_per_chirp: int | None = None) -> "RadarConfig":
        a = np.asarray(arr, dtype=float).ravel()
        if a.size != 7:
            raise ValueError(f"Expected 7 config values, got {a.size}")
        return RadarConfig(
            sample_rate=float(a[0]),
            signal_freq=float(a[1]),
            output_freq=float(a[2]),
            num_chirps=int(a[3]),
            chirp_bw=float(a[4]),
            ramp_time_s=float(a[5]),
            frame_length_ms=float(a[6]),
            samples_per_chirp=samples_per_chirp,
        )

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RadarConfig":
        """Load from a JSON payload.  Accepts both 'chirp_bw' and 'chirp_BW'."""
        chirp_bw = float(d.get("chirp_bw", d.get("chirp_BW", 500e6)))
        ramp_time_s = float(
            d.get("ramp_time_s", d.get("ramp_time_us", 500) / 1e6)
        )
        frame_length_ms = float(
            d.get("frame_length_ms", ramp_time_s * 1e3)
        )
        spc = d.get("samples_per_chirp", d.get("n_frame", d.get("N_frame")))
        return RadarConfig(
            sample_rate=float(d["sample_rate"]),
            signal_freq=float(d["signal_freq"]),
            output_freq=float(d["output_freq"]),
            num_chirps=int(d["num_chirps"]),
            chirp_bw=chirp_bw,
            ramp_time_s=ramp_time_s,
            frame_length_ms=frame_length_ms,
            samples_per_chirp=int(spc) if spc is not None else None,
        )


# ---------------------------------------------------------------------------
# Chirp matrix helpers
# ---------------------------------------------------------------------------

def check_chirp_matrix(matrix: np.ndarray, cfg: RadarConfig | None = None) -> np.ndarray:
    """Cast to complex64 and assert 2D shape."""
    m = np.asarray(matrix, dtype=np.complex64)
    if m.ndim != 2:
        raise ValueError(f"Chirp matrix must be 2D, got shape {m.shape}")
    if cfg is not None:
        if m.shape[0] != cfg.num_chirps:
            raise ValueError(
                f"Expected {cfg.num_chirps} chirps, got {m.shape[0]}"
            )
        if m.shape[1] != cfg.n_frame:
            raise ValueError(
                f"Expected {cfg.n_frame} samples/chirp, got {m.shape[1]}"
            )
    return m


def segment_buffer(
    raw: np.ndarray,
    *,
    num_chirps: int,
    samples_per_chirp: int,
    skip_chirps: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    """Slice a contiguous rx buffer into a (num_chirps, spc) matrix.

    Parameters
    ----------
    raw:
        Flat 1-D complex buffer from a single rx() call.
    skip_chirps:
        Number of leading chirps to discard (the first may start mid-period
        on a continuous sawtooth capture).

    Returns
    -------
    chirps : (num_chirps, spc) complex64, zero-padded if buffer too short
    valid  : (num_chirps,) bool, True where the full chirp was available
    """
    data = np.asarray(raw, dtype=np.complex64).ravel()
    chirps = np.zeros((num_chirps, samples_per_chirp), dtype=np.complex64)
    valid = np.zeros(num_chirps, dtype=bool)
    base = skip_chirps * samples_per_chirp
    for k in range(num_chirps):
        s = base + k * samples_per_chirp
        e = s + samples_per_chirp
        if s >= data.size:
            break
        chunk = data[s:e]
        chirps[k, : chunk.size] = chunk
        valid[k] = chunk.size == samples_per_chirp
    return chirps, valid


# ---------------------------------------------------------------------------
# DSP functions  (derived directly from Range_Doppler_Plot.py + NoTDD)
# ---------------------------------------------------------------------------

def apply_mti(chirp_matrix: np.ndarray) -> np.ndarray:
    """2-pulse canceller MTI filter.

    Directly ported from Range_Doppler_Plot.py lines 318-330.
    Removes static clutter (zero-Doppler) chirp by chirp using a phase
    correlation to compensate for any slow phase drift between pulses.
    """
    chirps = check_chirp_matrix(chirp_matrix)
    num_chirps, num_samples = chirps.shape
    out = np.zeros_like(chirps)
    for k in range(num_chirps - 1):
        c0 = chirps[k]
        c1 = chirps[k + 1]
        corr = np.correlate(c0, c1, "valid")
        phi = np.angle(corr[0]) if corr.size else 0.0
        out[k] = c1 - c0 * np.exp(-1j * phi)
    return out


def range_doppler_map(
    chirp_matrix: np.ndarray,
    *,
    window: bool = True,
    min_scale: float | None = None,
    max_scale: float | None = None,
) -> np.ndarray:
    """2D FFT -> range-Doppler magnitude map, log10 scaled.

    Directly from Range_Doppler_Plot.py `freq_process`:
        fftshift(abs(fft2(data))).T  then log10

    Parameters
    ----------
    window : bool
        Apply a 2D Hanning window before the FFT (default True).
        Reduces range/Doppler sidelobes from -13 dB (rectangular) to
        -32 dB, greatly improving target visibility near clutter/leakage.

    Returns
    -------
    np.ndarray, shape (samples_per_chirp, num_chirps), float32
        Range on axis-0, Doppler on axis-1.
    """
    chirps = check_chirp_matrix(chirp_matrix)
    if window:
        # Outer product of two Hanning windows: one per dimension
        w_doppler = np.hanning(chirps.shape[0]).astype(np.float32)
        w_range = np.hanning(chirps.shape[1]).astype(np.float32)
        chirps = chirps * w_doppler[:, np.newaxis] * w_range[np.newaxis, :]
    rd = np.fft.fftshift(np.abs(np.fft.fft2(chirps)))
    rd_log = np.log10(rd + 1e-10).T
    lo = rd_log.min() if min_scale is None else min_scale
    hi = rd_log.max() if max_scale is None else max_scale
    return np.clip(rd_log, lo, hi).astype(np.float32)


def range_profile(rd_map: np.ndarray) -> np.ndarray:
    """Collapse range-Doppler map to a 1-D range profile (max across Doppler).

    Parameters
    ----------
    rd_map : (n_range, n_doppler) float32
    """
    return np.asarray(rd_map, dtype=np.float32).max(axis=1)


def range_bin_for_distance(range_axis_m: np.ndarray, distance_m: float) -> int:
    """Return the nearest FFT range bin for a requested physical distance."""
    axis = np.asarray(range_axis_m, dtype=float).reshape(-1)
    if axis.size == 0:
        raise ValueError("range_axis_m must not be empty")
    return int(np.argmin(np.abs(axis - float(distance_m))))


def select_range_gate(
    profile: np.ndarray,
    *,
    manual: int | None = None,
    manual_m: float | None = None,
    min_bin: int | None = 0,
    min_range_m: float | None = None,
    range_axis_m: np.ndarray | None = None,
) -> int:
    """Return the range bin to gate for micro-Doppler extraction.

    Priority:
      1. manual bin
      2. manual distance in metres
      3. auto argmax subject to min-range filters

    When a physical range axis is available, auto mode prefers non-negative
    ranges by default so the mirrored negative-range half does not dominate.
    """
    prof = np.asarray(profile, dtype=np.float32).reshape(-1)
    if prof.size == 0:
        raise ValueError("profile must not be empty")

    if manual is not None:
        return int(np.clip(manual, 0, prof.size - 1))

    if manual_m is not None:
        if range_axis_m is None:
            raise ValueError("manual_m requires range_axis_m")
        return range_bin_for_distance(range_axis_m, manual_m)

    start = 0 if min_bin is None else int(np.clip(min_bin, 0, prof.size - 1))
    candidate_mask = np.zeros(prof.size, dtype=bool)
    candidate_mask[start:] = True

    if range_axis_m is not None:
        axis = np.asarray(range_axis_m, dtype=float).reshape(-1)
        if axis.size != prof.size:
            raise ValueError(
                f"range_axis_m length {axis.size} does not match profile length {prof.size}"
            )
        candidate_mask &= np.isfinite(axis)
        if min_range_m is not None:
            candidate_mask &= axis >= float(min_range_m)
        else:
            candidate_mask &= axis >= 0.0
    elif min_range_m is not None:
        raise ValueError("min_range_m requires range_axis_m")

    if not np.any(candidate_mask):
        candidate_mask[:] = False
        candidate_mask[start:] = True

    candidate_idx = np.flatnonzero(candidate_mask)
    return int(candidate_idx[np.argmax(prof[candidate_idx])])


def process_frame(
    chirp_matrix: np.ndarray,
    *,
    apply_mti_filter: bool = True,
    window: bool = True,
    manual_range_bin: int | None = None,
    manual_range_m: float | None = None,
    min_range_bin: int | None = 0,
    min_range_m: float | None = None,
    range_axis_m: np.ndarray | None = None,
    min_scale: float | None = None,
    max_scale: float | None = None,
) -> dict[str, Any]:
    """Full single-frame pipeline from chirp matrix to micro-Doppler slice.

    Parameters
    ----------
    chirp_matrix : (num_chirps, samples_per_chirp) complex
    apply_mti_filter : apply 2-pulse canceller before FFT
    manual_range_bin : fix the range gate by FFT bin
    manual_range_m : fix the range gate by physical distance in metres
    min_range_bin : bin-domain lower bound for auto gate search
    min_range_m : physical lower bound for auto gate search
    range_axis_m : range axis used for metre-based gating and reporting
    min_scale / max_scale : log10 colormap clip values

    Returns
    -------
    dict with keys:
        raw_chirps        : input matrix as complex64
        proc_chirps       : post-MTI matrix (or raw if MTI disabled)
        rd_map            : (n_range, n_doppler) float32 range-Doppler map
        range_profile     : (n_range,) float32
        range_bin         : int, selected range gate
        range_m           : float | None, selected physical range
        micro_doppler     : (n_doppler,) float32 Doppler slice at range_bin
    """
    raw = check_chirp_matrix(chirp_matrix)
    proc = apply_mti(raw) if apply_mti_filter else raw.copy()
    rd = range_doppler_map(proc, window=window, min_scale=min_scale, max_scale=max_scale)
    prof = range_profile(rd)
    rbin = select_range_gate(
        prof,
        manual=manual_range_bin,
        manual_m=manual_range_m,
        min_bin=min_range_bin,
        min_range_m=min_range_m,
        range_axis_m=range_axis_m,
    )
    range_m = None
    if range_axis_m is not None:
        axis = np.asarray(range_axis_m, dtype=float).reshape(-1)
        range_m = float(axis[rbin])
    return {
        "raw_chirps": raw,
        "proc_chirps": proc,
        "rd_map": rd,
        "range_profile": prof,
        "range_bin": rbin,
        "range_m": range_m,
        "micro_doppler": rd[rbin].astype(np.float32, copy=False),
    }


# ---------------------------------------------------------------------------
# Micro-Doppler history
# ---------------------------------------------------------------------------

class MicroDopplerHistory:
    """Rolling 2-D buffer of Doppler slices for micro-Doppler display.

    Shape: (history_len, doppler_bins)
    Oldest row at [0], newest at [-1].
    """

    def __init__(self, history_len: int, doppler_bins: int) -> None:
        self.data = np.zeros((history_len, doppler_bins), dtype=np.float32)

    def push(self, doppler_slice: np.ndarray) -> np.ndarray:
        """Append one Doppler slice; return the updated history array."""
        row = np.asarray(doppler_slice, dtype=np.float32).ravel()
        if row.size != self.data.shape[1]:
            raise ValueError(
                f"Expected {self.data.shape[1]} Doppler bins, got {row.size}"
            )
        self.data = np.roll(self.data, -1, axis=0)
        self.data[-1] = row
        return self.data


# ---------------------------------------------------------------------------
# Session save / load
# ---------------------------------------------------------------------------

def save_session(
    *,
    out_dir: Path | str,
    cfg: RadarConfig,
    raw_bursts: np.ndarray,   # (n_frames, n_chirps, spc) complex64
    spectrograms: np.ndarray, # (n_frames, n_doppler) float32
    session_name: str | None = None,
    labels: dict | None = None,
    metadata: dict | None = None,
) -> Path:
    """Write a SENTINEL recording session to disk.

    Directory layout::

        <out_dir>/<session_name>/
            config.json
            labels.json
            raw_bursts.npy       -- (n_frames, n_chirps, spc) complex64
            spectrograms.npy     -- (n_frames, n_doppler) float32
            config_adi.npy       -- 7-element ADI-compatible config array
    """
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    name = session_name or datetime.now().strftime("%Y%m%d_%H%M%S")
    sdir = base / name
    sdir.mkdir(parents=True, exist_ok=True)

    payload = cfg.to_dict()
    if metadata:
        payload["metadata"] = metadata
    (sdir / "config.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (sdir / "labels.json").write_text(
        json.dumps(labels or {"segments": []}, indent=2), encoding="utf-8"
    )
    np.save(sdir / "raw_bursts.npy", np.asarray(raw_bursts, dtype=np.complex64))
    np.save(sdir / "spectrograms.npy", np.asarray(spectrograms, dtype=np.float32))
    np.save(sdir / "config_adi.npy", cfg.to_adi_array())

    return sdir


def load_capture(path: Path | str) -> tuple[np.ndarray, RadarConfig]:
    """Load a saved capture.

    Accepts:
    - SENTINEL session directory (config.json + raw_bursts.npy)
    - Legacy session directory (older JSON key names)
    - ADI-format pair: ``capture.npy`` + ``capture_config.npy``

    Returns
    -------
    raw_bursts : (n_frames, n_chirps, spc) complex64
    cfg        : RadarConfig
    """
    p = Path(path).expanduser().resolve()

    if p.is_dir():
        bursts = np.load(p / "raw_bursts.npy")
        cfg_json = p / "config.json"
        if cfg_json.exists():
            cfg = RadarConfig.from_dict(
                json.loads(cfg_json.read_text(encoding="utf-8"))
            )
        else:
            cfg_npy = p / "config_adi.npy"
            if not cfg_npy.exists():
                cfg_npy = p / "raw_bursts_config.npy"
            cfg = RadarConfig.from_adi_array(
                np.load(cfg_npy), samples_per_chirp=bursts.shape[-1]
            )
        return bursts, cfg

    # flat .npy file
    bursts = np.load(p)
    cfg_path = p.with_name(f"{p.stem}_config.npy")
    cfg = RadarConfig.from_adi_array(
        np.load(cfg_path), samples_per_chirp=bursts.shape[-1]
    )
    return bursts, cfg
