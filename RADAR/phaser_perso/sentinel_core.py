from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

import numpy as np


_C = 3e8


@dataclass(frozen=True)
class RadarConfig:
    sample_rate: float
    signal_freq: float
    output_freq: float
    num_chirps: int
    chirp_bw: float
    ramp_time_s: float
    frame_length_ms: float
    samples_per_chirp: int | None = None

    @property
    def effective_samples_per_chirp(self) -> int:
        if self.samples_per_chirp is not None:
            return int(self.samples_per_chirp)
        return int(round((self.frame_length_ms / 1e3) * self.sample_rate))

    @property
    def wavelength(self) -> float:
        return _C / self.output_freq

    @property
    def slope(self) -> float:
        return self.chirp_bw / self.ramp_time_s

    @property
    def pri_s(self) -> float:
        return self.frame_length_ms / 1e3

    @property
    def prf(self) -> float:
        return 1.0 / self.pri_s

    @property
    def range_resolution_m(self) -> float:
        return _C / (2.0 * self.chirp_bw)

    @property
    def velocity_resolution_m_s(self) -> float:
        return self.wavelength / (2.0 * self.num_chirps * self.pri_s)

    @property
    def max_unambiguous_velocity_m_s(self) -> float:
        return (self.prf / 2.0) * self.wavelength / 2.0

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "effective_samples_per_chirp": self.effective_samples_per_chirp,
                "range_resolution_m": self.range_resolution_m,
                "velocity_resolution_m_s": self.velocity_resolution_m_s,
                "max_unambiguous_velocity_m_s": self.max_unambiguous_velocity_m_s,
            }
        )
        return payload


def export_adi_config(config: RadarConfig) -> np.ndarray:
    return np.asarray(
        [
            config.sample_rate,
            config.signal_freq,
            config.output_freq,
            config.num_chirps,
            config.chirp_bw,
            config.ramp_time_s,
            config.frame_length_ms,
        ],
        dtype=float,
    )


def import_adi_config(
    config_array: np.ndarray,
    *,
    samples_per_chirp: int | None = None,
) -> RadarConfig:
    flat = np.asarray(config_array, dtype=float).reshape(-1)
    if flat.size != 7:
        raise ValueError(f"Expected 7 config values, got {flat.size}")
    return RadarConfig(
        sample_rate=float(flat[0]),
        signal_freq=float(flat[1]),
        output_freq=float(flat[2]),
        num_chirps=int(flat[3]),
        chirp_bw=float(flat[4]),
        ramp_time_s=float(flat[5]),
        frame_length_ms=float(flat[6]),
        samples_per_chirp=samples_per_chirp,
    )


def range_axis(config: RadarConfig) -> np.ndarray:
    samples = config.effective_samples_per_chirp
    freq = np.linspace(-config.sample_rate / 2, config.sample_rate / 2, samples)
    return (freq - config.signal_freq) * _C / (2.0 * config.slope)


def velocity_axis(config: RadarConfig) -> np.ndarray:
    return np.linspace(
        -config.max_unambiguous_velocity_m_s,
        config.max_unambiguous_velocity_m_s,
        config.num_chirps,
    )


def ensure_chirp_matrix(
    chirp_matrix: np.ndarray,
    *,
    num_chirps: int | None = None,
    samples_per_chirp: int | None = None,
) -> np.ndarray:
    matrix = np.asarray(chirp_matrix, dtype=np.complex64)
    if matrix.ndim != 2:
        raise ValueError(f"Expected a 2D chirp matrix, got shape {matrix.shape}")
    if num_chirps is not None and matrix.shape[0] != num_chirps:
        raise ValueError(
            f"Expected {num_chirps} chirps, got {matrix.shape[0]}"
        )
    if samples_per_chirp is not None and matrix.shape[1] != samples_per_chirp:
        raise ValueError(
            f"Expected {samples_per_chirp} samples/chirp, got {matrix.shape[1]}"
        )
    return matrix


def segment_continuous_capture(
    raw_samples: np.ndarray,
    *,
    num_chirps: int,
    samples_per_chirp: int,
    skip_chirps: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    data = np.asarray(raw_samples, dtype=np.complex64).reshape(-1)
    chirps = np.zeros((num_chirps, samples_per_chirp), dtype=np.complex64)
    valid = np.zeros(num_chirps, dtype=bool)

    base = skip_chirps * samples_per_chirp
    for chirp_idx in range(num_chirps):
        start = base + chirp_idx * samples_per_chirp
        stop = start + samples_per_chirp
        if start >= data.size:
            break
        chunk = data[start:stop]
        chirps[chirp_idx, : chunk.size] = chunk
        valid[chirp_idx] = chunk.size == samples_per_chirp

    return chirps, valid


def apply_2pulse_mti(chirp_matrix: np.ndarray) -> np.ndarray:
    chirps = ensure_chirp_matrix(chirp_matrix)
    filtered = np.zeros_like(chirps)
    for chirp_idx in range(chirps.shape[0] - 1):
        current = chirps[chirp_idx]
        nxt = chirps[chirp_idx + 1]
        corr = np.correlate(current, nxt, "valid")
        phase = np.angle(corr[0]) if corr.size else 0.0
        filtered[chirp_idx] = nxt - current * np.exp(-1j * phase)
    return filtered


def range_doppler_fft(
    chirp_matrix: np.ndarray,
    *,
    min_scale: float | None = None,
    max_scale: float | None = None,
) -> np.ndarray:
    chirps = ensure_chirp_matrix(chirp_matrix)
    rd = np.fft.fftshift(np.abs(np.fft.fft2(chirps)))
    rd_db = np.log10(rd + 1e-10).T
    if min_scale is not None or max_scale is not None:
        lower = rd_db.min() if min_scale is None else min_scale
        upper = rd_db.max() if max_scale is None else max_scale
        rd_db = np.clip(rd_db, lower, upper)
    return rd_db.astype(np.float32)


def range_profile(range_doppler: np.ndarray) -> np.ndarray:
    rd = np.asarray(range_doppler, dtype=np.float32)
    if rd.ndim != 2:
        raise ValueError(f"Expected 2D range-Doppler data, got {rd.shape}")
    return rd.max(axis=1)


def cfar_range_profile(
    profile: np.ndarray,
    *,
    guard_cells: int = 15,
    ref_cells: int = 16,
    bias: float = 25.0,
    method: str = "average",
) -> tuple[np.ndarray, np.ndarray]:
    from target_detection_dbfs import cfar

    thresholds, masked = cfar(
        np.asarray(profile, dtype=np.float32),
        num_guard_cells=guard_cells,
        num_ref_cells=ref_cells,
        bias=bias,
        cfar_method=method,
    )
    filled_thresholds = np.ma.filled(thresholds, fill_value=np.min(profile))
    masked_targets = np.ma.getmaskarray(masked)
    return np.asarray(filled_thresholds, dtype=np.float32), masked_targets


def select_range_gate(
    profile: np.ndarray,
    *,
    manual_range_bin: int | None = None,
    min_range_bin: int = 0,
) -> int:
    if manual_range_bin is not None:
        return int(np.clip(manual_range_bin, 0, len(profile) - 1))

    start = int(np.clip(min_range_bin, 0, len(profile) - 1))
    if start >= len(profile):
        return len(profile) - 1
    return int(np.argmax(profile[start:]) + start)


def process_frame(
    chirp_matrix: np.ndarray,
    *,
    apply_mti: bool = True,
    apply_cfar: bool = False,
    manual_range_bin: int | None = None,
    min_range_bin: int = 0,
    cfar_guard: int = 15,
    cfar_ref: int = 16,
    cfar_bias: float = 25.0,
    min_scale: float | None = None,
    max_scale: float | None = None,
) -> dict[str, Any]:
    raw = ensure_chirp_matrix(chirp_matrix)
    processed = apply_2pulse_mti(raw) if apply_mti else raw.copy()
    rd = range_doppler_fft(processed, min_scale=min_scale, max_scale=max_scale)
    profile = range_profile(rd)

    cfar_threshold = None
    cfar_targets = None
    if apply_cfar:
        cfar_threshold, cfar_targets = cfar_range_profile(
            profile,
            guard_cells=cfar_guard,
            ref_cells=cfar_ref,
            bias=cfar_bias,
        )

    selected_range_bin = select_range_gate(
        profile,
        manual_range_bin=manual_range_bin,
        min_range_bin=min_range_bin,
    )
    micro_doppler = rd[selected_range_bin].astype(np.float32, copy=False)

    return {
        "raw_chirps": raw,
        "processed_chirps": processed,
        "range_doppler": rd,
        "range_profile": profile.astype(np.float32, copy=False),
        "cfar_threshold": cfar_threshold,
        "cfar_targets": cfar_targets,
        "selected_range_bin": selected_range_bin,
        "micro_doppler": micro_doppler,
    }


class MicroDopplerHistory:
    def __init__(self, history_length: int, doppler_bins: int):
        self.data = np.zeros((history_length, doppler_bins), dtype=np.float32)

    def append(self, doppler_slice: np.ndarray) -> np.ndarray:
        row = np.asarray(doppler_slice, dtype=np.float32).reshape(-1)
        if row.size != self.data.shape[1]:
            raise ValueError(
                f"Expected {self.data.shape[1]} Doppler bins, got {row.size}"
            )
        self.data = np.roll(self.data, -1, axis=0)
        self.data[-1] = row
        return self.data


def save_recording_session(
    *,
    base_dir: str | Path,
    config: RadarConfig,
    raw_bursts: np.ndarray,
    spectrogram_rows: np.ndarray,
    session_name: str | None = None,
    labels: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    raw_ch0: np.ndarray | None = None,
    raw_ch1: np.ndarray | None = None,
    valid_chirps: np.ndarray | None = None,
) -> Path:
    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    if session_name is None:
        session_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = base_path / session_name
    session_dir.mkdir(parents=True, exist_ok=True)

    payload = config.to_json()
    if metadata:
        payload["metadata"] = metadata
    payload["files"] = {
        "raw_bursts.npy": "[n_frames, n_chirps, n_samples] complex64",
        "spectrograms.npy": "[n_frames, n_doppler_bins] float32",
        "raw_bursts_config.npy": "ADI-compatible capture config array",
    }
    if raw_ch0 is not None:
        payload["files"]["raw_ch0.npy"] = "[n_frames, n_chirps, n_samples] complex64"
    if raw_ch1 is not None:
        payload["files"]["raw_ch1.npy"] = "[n_frames, n_chirps, n_samples] complex64"
    if valid_chirps is not None:
        payload["files"]["valid_chirps.npy"] = "[n_frames, n_chirps] bool"

    (session_dir / "config.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    (session_dir / "labels.json").write_text(
        json.dumps(labels or {"segments": []}, indent=2),
        encoding="utf-8",
    )

    np.save(session_dir / "raw_bursts.npy", np.asarray(raw_bursts, dtype=np.complex64))
    np.save(
        session_dir / "spectrograms.npy",
        np.asarray(spectrogram_rows, dtype=np.float32),
    )
    np.save(session_dir / "raw_bursts_config.npy", export_adi_config(config))

    if raw_ch0 is not None:
        np.save(session_dir / "raw_ch0.npy", np.asarray(raw_ch0, dtype=np.complex64))
    if raw_ch1 is not None:
        np.save(session_dir / "raw_ch1.npy", np.asarray(raw_ch1, dtype=np.complex64))
    if valid_chirps is not None:
        np.save(session_dir / "valid_chirps.npy", np.asarray(valid_chirps, dtype=bool))

    return session_dir


def load_adi_capture(path: str | Path) -> tuple[np.ndarray, RadarConfig]:
    raw_path = Path(path)
    if raw_path.is_dir():
        raw_bursts = np.load(raw_path / "raw_bursts.npy")
        config_json = raw_path / "config.json"
        if config_json.exists():
            payload = json.loads(config_json.read_text(encoding="utf-8"))
            config = RadarConfig(
                sample_rate=float(payload["sample_rate"]),
                signal_freq=float(payload["signal_freq"]),
                output_freq=float(payload["output_freq"]),
                num_chirps=int(payload["num_chirps"]),
                chirp_bw=float(payload["chirp_bw"]),
                ramp_time_s=float(payload["ramp_time_s"]),
                frame_length_ms=float(payload["frame_length_ms"]),
                samples_per_chirp=int(raw_bursts.shape[-1]),
            )
            return raw_bursts, config
        config_array = np.load(raw_path / "raw_bursts_config.npy")
        return raw_bursts, import_adi_config(
            config_array,
            samples_per_chirp=raw_bursts.shape[-1],
        )

    raw_bursts = np.load(raw_path)
    config_path = raw_path.with_name(f"{raw_path.stem}_config.npy")
    config_array = np.load(config_path)
    return raw_bursts, import_adi_config(
        config_array,
        samples_per_chirp=raw_bursts.shape[-1],
    )
