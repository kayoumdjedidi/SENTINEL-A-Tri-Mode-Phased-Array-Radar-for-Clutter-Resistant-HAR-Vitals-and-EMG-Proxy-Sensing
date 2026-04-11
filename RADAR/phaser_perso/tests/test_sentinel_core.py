import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


THIS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = THIS_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


from sentinel_core import (  # noqa: E402
    RadarConfig,
    export_adi_config,
    load_adi_capture,
    process_frame,
    save_recording_session,
    segment_continuous_capture,
)
from sentinel_backends import RuntimeRadarConfig, TDDBurstBackend  # noqa: E402
from sentinel_mode_a import build_arg_parser, build_runtime_config  # noqa: E402


def test_segment_continuous_capture_pads_partial_tail_and_marks_validity():
    samples_per_chirp = 4
    num_chirps = 3
    skip_chirps = 1
    raw = np.arange(14, dtype=np.complex64)

    chirps, valid = segment_continuous_capture(
        raw,
        num_chirps=num_chirps,
        samples_per_chirp=samples_per_chirp,
        skip_chirps=skip_chirps,
    )

    assert chirps.shape == (3, 4)
    assert chirps.dtype == np.complex64
    assert valid.tolist() == [True, True, False]
    np.testing.assert_array_equal(chirps[0], np.array([4, 5, 6, 7], dtype=np.complex64))
    np.testing.assert_array_equal(chirps[1], np.array([8, 9, 10, 11], dtype=np.complex64))
    np.testing.assert_array_equal(chirps[2], np.array([12, 13, 0, 0], dtype=np.complex64))


def test_process_frame_returns_backend_neutral_products():
    num_chirps = 8
    samples_per_chirp = 16
    beat_bin = 3
    doppler_bin = 2

    fast = np.arange(samples_per_chirp)
    slow = np.arange(num_chirps)
    chirps = np.exp(
        1j
        * (
            2 * np.pi * beat_bin * fast[np.newaxis, :] / samples_per_chirp
            + 2 * np.pi * doppler_bin * slow[:, np.newaxis] / num_chirps
        )
    ).astype(np.complex64)

    result = process_frame(
        chirps,
        apply_mti=False,
        apply_cfar=False,
        manual_range_bin=None,
        min_range_bin=1,
    )

    assert result["range_doppler"].shape == (samples_per_chirp, num_chirps)
    assert result["range_profile"].shape == (samples_per_chirp,)
    assert result["micro_doppler"].shape == (num_chirps,)
    assert result["selected_range_bin"] == int(np.argmax(result["range_profile"][1:]) + 1)
    assert result["cfar_threshold"] is None


def test_save_recording_session_writes_expected_files_and_config(tmp_path):
    config = RadarConfig(
        sample_rate=4e6,
        signal_freq=100e3,
        output_freq=10.25e9,
        num_chirps=64,
        chirp_bw=500e6,
        ramp_time_s=500e-6,
        frame_length_ms=0.5,
    )
    raw_bursts = np.ones((2, 64, 8), dtype=np.complex64)
    spectrograms = np.ones((2, 64), dtype=np.float32)

    session_dir = save_recording_session(
        base_dir=tmp_path,
        config=config,
        raw_bursts=raw_bursts,
        spectrogram_rows=spectrograms,
        session_name="unit_test_session",
    )

    assert session_dir.name == "unit_test_session"
    assert (session_dir / "config.json").exists()
    assert (session_dir / "labels.json").exists()
    assert (session_dir / "raw_bursts.npy").exists()
    assert (session_dir / "spectrograms.npy").exists()
    assert (session_dir / "raw_bursts_config.npy").exists()
    np.testing.assert_allclose(
        np.load(session_dir / "raw_bursts_config.npy"),
        export_adi_config(config),
    )

    loaded_bursts, loaded_config = load_adi_capture(session_dir)
    np.testing.assert_array_equal(loaded_bursts, raw_bursts)
    assert loaded_config.effective_samples_per_chirp == raw_bursts.shape[-1]
    np.testing.assert_allclose(export_adi_config(loaded_config), export_adi_config(config))


def test_mode_a_parser_supports_tdd_and_continuous():
    parser = build_arg_parser()

    args = parser.parse_args(["--acq", "tdd", "--frames", "5"])
    assert args.acq == "tdd"
    assert args.frames == 5

    args = parser.parse_args(["--acq", "continuous"])
    assert args.acq == "continuous"

    with pytest.raises(SystemExit):
        parser.parse_args(["--acq", "invalid"])


def test_tdd_auto_profile_uses_internal_reference_defaults():
    parser = build_arg_parser()
    args = parser.parse_args(["--acq", "tdd"])

    runtime = build_runtime_config(args)

    assert runtime.ramp_time_us == 300
    assert runtime.num_chirps == 64
    assert runtime.tdd_sync_external is False
    assert runtime.tdd_ext_capture is False


def test_tdd_custom_profile_preserves_explicit_runtime_values():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--acq",
            "tdd",
            "--tdd-profile",
            "custom",
            "--ramp-time-us",
            "500",
            "--num-chirps",
            "128",
            "--tdd-sync-external",
            "--tdd-ext-capture",
        ]
    )

    runtime = build_runtime_config(args)

    assert runtime.ramp_time_us == 500
    assert runtime.num_chirps == 128
    assert runtime.tdd_sync_external is True
    assert runtime.tdd_ext_capture is True


@pytest.mark.parametrize(
    ("tdd_sync_external", "tdd_ext_capture", "expects_gpio_burst"),
    [
        (False, False, False),
        (True, True, True),
    ],
)
def test_tdd_backend_gpio_burst_matches_trigger_mode(
    monkeypatch,
    tdd_sync_external,
    tdd_ext_capture,
    expects_gpio_burst,
):
    backend = TDDBurstBackend(
        RuntimeRadarConfig(
            num_chirps=2,
            tdd_sync_external=tdd_sync_external,
            tdd_ext_capture=tdd_ext_capture,
        )
    )
    backend.core_config = RadarConfig(
        sample_rate=4e6,
        signal_freq=100e3,
        output_freq=10.25e9,
        num_chirps=2,
        chirp_bw=500e6,
        ramp_time_s=500e-6,
        frame_length_ms=0.7,
        samples_per_chirp=4,
    )
    backend.good_ramp_samples = 4
    backend.num_samples_frame = 5
    backend.start_offset_samples = 0
    backend.my_phaser = object()
    backend.tdd = SimpleNamespace(frame_length_ms=0.7)

    class FakeSdr:
        rx_buffer_size = 1024

        def rx(self):
            ch0 = np.arange(10, dtype=np.float32).astype(np.complex64)
            ch1 = np.zeros(10, dtype=np.complex64)
            return [ch0, ch1]

    backend.my_sdr = FakeSdr()
    burst_calls = []
    monkeypatch.setattr(
        "sentinel_backends._set_burst_gpio",
        lambda phaser: burst_calls.append(phaser),
    )

    capture = backend.capture_chirp_matrix()

    assert capture.diagnostics["uses_gpio_burst"] is expects_gpio_burst
    assert bool(burst_calls) is expects_gpio_burst
