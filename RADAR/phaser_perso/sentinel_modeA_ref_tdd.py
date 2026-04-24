#!/usr/bin/env python3
"""Reference-faithful FMCW Range-Doppler plot for CN0566.

This is intentionally much closer to ADI's `reference/Range_Doppler_Plot.py`
than the SENTINEL GUI runtimes.  The point is to test the actual reference
signal chain first:

1. TDD-synchronised chirp burst capture
2. Slice the contiguous Pluto buffer into chirp columns using the same timing
3. Optional 2-pulse MTI
4. Plain 2D FFT -> range-Doppler image

It does not add auto-gating, background subtraction, or adaptive display logic.
If this script works and the richer SENTINEL modes do not, the problem is in our
added processing/UI layers.  If this script also fails, the issue is lower in
the acquisition path.

Usage
-----
  python sentinel_modeA_ref_tdd.py
  python sentinel_modeA_ref_tdd.py --sdr-uri ip:192.168.2.1
  python sentinel_modeA_ref_tdd.py --save-data --save-path ref_capture.npy
"""
from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

plt.close("all")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel_modeA_ref_tdd",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rpi-uri", default="ip:phaser.local", help="CN0566 / Pi IIO URI")
    p.add_argument(
        "--sdr-uri",
        default="auto",
        help="Pluto URI. 'auto' tries direct Pluto first, then forwarded 50901",
    )
    p.add_argument(
        "--tdd-uri",
        default=None,
        help="Pluto GPIO/TDD URI; defaults to the resolved --sdr-uri",
    )
    p.add_argument("--sample-rate", type=float, default=4e6, help="Pluto sample rate")
    p.add_argument("--center-freq", type=float, default=2.1e9, help="Pluto RX/TX LO")
    p.add_argument("--signal-freq", type=float, default=100e3, help="Baseband TX tone")
    p.add_argument("--rx-gain", type=int, default=60, help="Pluto RX gain dB")
    p.add_argument("--tx-gain", type=int, default=0, help="Pluto TX gain dB on active channel")
    p.add_argument("--output-freq", type=float, default=10.25e9, help="Radiated RF centre")
    p.add_argument("--bw", type=float, default=500e6, help="Chirp bandwidth")
    p.add_argument("--ramp", type=int, default=300, help="Ramp time in microseconds")
    p.add_argument("--chirps", type=int, default=256, help="Chirps per burst")
    p.add_argument("--max-range", type=float, default=30.0, help="Displayed max range in metres")
    p.add_argument("--min-scale", type=float, default=0.0, help="Lower log10 clip level")
    p.add_argument("--max-scale", type=float, default=8.0, help="Upper log10 clip level")
    p.add_argument(
        "--mti",
        action="store_true",
        help="Enable 2-pulse canceller MTI before the 2D FFT",
    )
    p.add_argument(
        "--begin-offset-frac",
        type=float,
        default=0.1,
        help="Fraction of each ramp to skip before collecting good samples",
    )
    p.add_argument(
        "--save-data",
        action="store_true",
        help="Save each synchronised chirp matrix for offline processing",
    )
    p.add_argument(
        "--save-path",
        type=Path,
        default=Path("ref_capture.npy"),
        help="Path for saved burst matrices; config is written next to it",
    )
    p.add_argument(
        "--frames",
        type=int,
        default=0,
        help="Number of capture frames to run; 0 means run until Ctrl+C",
    )
    p.add_argument(
        "--no-plot",
        action="store_true",
        help="Skip matplotlib display and just capture/save data",
    )
    p.add_argument(
        "--tdd-arm-delay-s",
        type=float,
        default=0.5,
        help="Seconds to wait after starting rx() before pulsing gpio_burst",
    )
    p.add_argument(
        "--tdd-rx-timeout-ms",
        type=int,
        default=30000,
        help="IIO rx timeout for the TDD burst capture",
    )
    return p


def next_power_of_two(n: int) -> int:
    return 1 << math.ceil(math.log2(max(1, n)))


def open_pluto(adi_module, requested_uri: str):
    if requested_uri == "auto":
        candidates = ["ip:192.168.2.1", "ip:phaser.local:50901"]
    else:
        candidates = [requested_uri]

    errors: list[str] = []
    for uri in candidates:
        try:
            return adi_module.ad9361(uri=uri), uri
        except Exception as exc:  # pragma: no cover - hardware/environment dependent
            errors.append(f"{uri}: {exc}")
    raise RuntimeError(
        "Could not open Pluto on any candidate URI.\n" + "\n".join(errors)
    )


def apply_mti(rx_bursts: np.ndarray) -> np.ndarray:
    chirps = np.asarray(rx_bursts, dtype=np.complex64)
    out = np.zeros_like(chirps)
    for chirp in range(chirps.shape[0] - 1):
        chirp_i = chirps[chirp, :]
        chirp_i1 = chirps[chirp + 1, :]
        corr = np.correlate(chirp_i, chirp_i1, "valid")
        angle_diff = np.angle(corr[0]) if corr.size else 0.0
        out[chirp, :] = chirp_i1 - chirp_i * np.exp(-1j * angle_diff)
    return out


def freq_process(data: np.ndarray, *, min_scale: float, max_scale: float) -> np.ndarray:
    rx_bursts_fft = np.fft.fftshift(np.abs(np.fft.fft2(data)))
    range_doppler_data = np.log10(np.maximum(rx_bursts_fft, 1e-12)).T
    return np.clip(range_doppler_data, min_scale, max_scale)


def get_radar_data(
    *,
    my_sdr,
    my_phaser,
    start_offset_samples: int,
    num_bursts: int,
    n_frame: int,
    good_ramp_samples: int,
    arm_delay_s: float,
) -> np.ndarray:
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def _rx() -> None:
        try:
            result["data"] = my_sdr.rx()
        except BaseException as exc:  # noqa: BLE001 - propagate hardware error
            errors.append(exc)

    rx_thread = threading.Thread(target=_rx, daemon=True)
    rx_thread.start()
    time.sleep(max(0.0, arm_delay_s))

    for value in (0, 1, 0):
        my_phaser._gpios.gpio_burst = value

    rx_thread.join(timeout=10.0)
    if errors:
        raise errors[0]
    if "data" not in result:
        raise TimeoutError("TDD rx() did not return after gpio_burst pulse")

    data = result["data"]
    chan1 = np.asarray(data[0], dtype=np.complex64)
    chan2 = np.asarray(data[1], dtype=np.complex64)
    sum_data = chan1 + chan2

    rx_bursts = np.zeros((num_bursts, good_ramp_samples), dtype=np.complex64)
    for burst in range(num_bursts):
        start_index = start_offset_samples + burst * n_frame
        stop_index = start_index + good_ramp_samples
        rx_bursts[burst] = sum_data[start_index:stop_index]
    return rx_bursts


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import adi

    def stage(msg: str) -> None:
        print(f"[ref] {msg}", flush=True)

    print(adi.__version__, flush=True)
    sample_rate = float(args.sample_rate)
    center_freq = float(args.center_freq)
    signal_freq = float(args.signal_freq)
    rx_gain = int(args.rx_gain)
    tx_gain = int(args.tx_gain)
    output_freq = float(args.output_freq)
    chirp_bw = float(args.bw)
    ramp_time = int(args.ramp)
    num_chirps = int(args.chirps)

    stage(f"Opening Pluto on {args.sdr_uri} ...")
    my_sdr, sdr_uri = open_pluto(adi, args.sdr_uri)
    my_sdr._ctx.set_timeout(int(args.tdd_rx_timeout_ms))
    stage(f"Opening CN0566 on {args.rpi_uri} ...")
    my_phaser = adi.CN0566(uri=args.rpi_uri, sdr=my_sdr)

    print(f"[ref] Pluto URI: {sdr_uri}", flush=True)
    tdd_uri = args.tdd_uri or sdr_uri
    print(f"[ref] TDD URI: {tdd_uri}", flush=True)
    if "50901" in sdr_uri:
        print(
            "[ref] WARNING: using the forwarded Pluto path. "
            "If TDD later times out at rx(), that is a transport limitation, "
            "not a range-Doppler math issue."
        , flush=True)

    my_phaser.configure(device_mode="rx")
    my_phaser.element_spacing = 0.014
    my_phaser.load_gain_cal()
    my_phaser.load_phase_cal()
    for i in range(8):
        my_phaser.set_chan_phase(i, 0)
        my_phaser.set_chan_gain(i, 127, apply_cal=True)

    my_phaser._gpios.gpio_tx_sw = 0
    my_phaser._gpios.gpio_vctrl_1 = 1
    my_phaser._gpios.gpio_vctrl_2 = 1

    my_sdr.sample_rate = int(sample_rate)
    my_sdr.rx_lo = int(center_freq)
    my_sdr.rx_enabled_channels = [0, 1]
    my_sdr.gain_control_mode_chan0 = "manual"
    my_sdr.gain_control_mode_chan1 = "manual"
    my_sdr.rx_hardwaregain_chan0 = rx_gain
    my_sdr.rx_hardwaregain_chan1 = rx_gain

    my_sdr.tx_lo = int(center_freq)
    my_sdr.tx_enabled_channels = [0, 1]
    my_sdr.tx_cyclic_buffer = True
    my_sdr.tx_hardwaregain_chan0 = -88
    my_sdr.tx_hardwaregain_chan1 = tx_gain

    vco_freq = int(output_freq + signal_freq + center_freq)
    num_steps = int(ramp_time)
    my_phaser.frequency = int(vco_freq / 4)
    my_phaser.freq_dev_range = int(chirp_bw / 4)
    my_phaser.freq_dev_step = int((chirp_bw / 4) / num_steps)
    my_phaser.freq_dev_time = ramp_time
    my_phaser.delay_word = 4095
    my_phaser.delay_clk = "PFD"
    my_phaser.delay_start_en = 0
    my_phaser.ramp_delay_en = 0
    my_phaser.trig_delay_en = 0
    my_phaser.ramp_mode = "single_sawtooth_burst"
    my_phaser.sing_ful_tri = 0
    my_phaser.tx_trig_en = 1
    my_phaser.enable = 0

    sdr_pins = adi.one_bit_adc_dac(tdd_uri)
    sdr_pins.gpio_tdd_ext_sync = True
    tdd = adi.tddn(tdd_uri)
    sdr_pins.gpio_phaser_enable = True
    tdd.enable = False
    tdd.sync_external = True
    tdd.startup_delay_ms = 0
    pri_ms = ramp_time / 1e3 + 0.2
    tdd.frame_length_ms = pri_ms
    tdd.burst_count = num_chirps
    for idx in range(3):
        tdd.channel[idx].enable = True
        tdd.channel[idx].polarity = False
        tdd.channel[idx].on_raw = 0
        tdd.channel[idx].off_raw = 10
    tdd.enable = True

    ramp_time = int(my_phaser.freq_dev_time)
    ramp_time_s = ramp_time / 1e6
    begin_offset_time = float(args.begin_offset_frac) * ramp_time_s
    good_ramp_samples = int((ramp_time_s - begin_offset_time) * sample_rate)
    start_offset_time = tdd.channel[0].on_ms / 1e3 + begin_offset_time
    start_offset_samples = int(start_offset_time * sample_rate)

    n_frame = int(tdd.frame_length_ms / 1000 * sample_rate)
    total_time_ms = tdd.frame_length_ms * num_chirps
    buffer_size = next_power_of_two((start_offset_samples + num_chirps * n_frame))
    buffer_time_ms = buffer_size / sample_rate * 1000.0
    my_sdr.rx_buffer_size = buffer_size

    c = 3e8
    wavelength = c / output_freq
    slope = chirp_bw / ramp_time_s
    freq = np.linspace(-sample_rate / 2, sample_rate / 2, n_frame)
    dist = (freq - signal_freq) * c / (2 * slope)
    prf = 1.0 / (tdd.frame_length_ms / 1e3)
    max_doppler_vel = (prf / 2.0) * wavelength / 2.0
    r_res = c / (2 * chirp_bw)
    v_res = wavelength / (2 * num_chirps * (tdd.frame_length_ms / 1e3))

    print(
        f"[ref] rf={output_freq/1e9:.4f} GHz  bw={chirp_bw/1e6:.0f} MHz  "
        f"ramp={ramp_time} us  chirps={num_chirps}\n"
        f"[ref] PRI={tdd.frame_length_ms:.3f} ms  N_frame={n_frame}  "
        f"good_ramp_samples={good_ramp_samples}  start_offset_samples={start_offset_samples}\n"
        f"[ref] rx_buffer_size={buffer_size}  buffer_time={buffer_time_ms:.1f} ms  "
        f"R_res={r_res:.3f} m  v_res={v_res:.3f} m/s"
    , flush=True)

    n = 2 ** 18
    ts = 1.0 / sample_rate
    t = np.arange(0, n * ts, ts)
    i = np.cos(2 * np.pi * t * signal_freq) * 2**14
    q = np.sin(2 * np.pi * t * signal_freq) * 2**14
    iq = (0.9 * (i + 1j * q)).astype(np.complex64)
    my_sdr.tx([iq, iq])

    saved_bursts: list[np.ndarray] = []

    stage("Capturing first burst ...")
    raw = get_radar_data(
        my_sdr=my_sdr,
        my_phaser=my_phaser,
        start_offset_samples=start_offset_samples,
        num_bursts=num_chirps,
        n_frame=n_frame,
        good_ramp_samples=good_ramp_samples,
        arm_delay_s=args.tdd_arm_delay_s,
    )
    stage("First burst captured")
    processed = apply_mti(raw) if args.mti else raw
    radar_data = freq_process(processed, min_scale=args.min_scale, max_scale=args.max_scale)

    im = None
    if not args.no_plot:
        fig, ax = plt.subplots(figsize=(14, 7))
        extent = [-max_doppler_vel, max_doppler_vel, dist.min(), dist.max()]
        try:
            im = ax.imshow(
                radar_data,
                aspect="auto",
                extent=extent,
                origin="lower",
                cmap=matplotlib.colormaps.get_cmap("inferno"),
            )
        except Exception:
            from matplotlib.cm import get_cmap

            im = ax.imshow(
                radar_data,
                aspect="auto",
                extent=extent,
                origin="lower",
                cmap=get_cmap("inferno"),
            )
        ax.set_title("Reference Range Doppler Spectrum", fontsize=20)
        ax.set_xlabel("Velocity [m/s]", fontsize=16)
        ax.set_ylabel("Range [m]", fontsize=16)
        ax.set_xlim([-10, 10])
        ax.set_ylim([0, args.max_range])
        ax.set_yticks(np.arange(0, args.max_range + 1e-9, max(1, args.max_range / 10)))

    if args.save_data:
        saved_bursts.append(raw.copy())

    if args.frames:
        frame_budget = max(0, args.frames - 1)
        print(f"[ref] Running for {args.frames} frame(s)", flush=True)
    else:
        frame_budget = None
        print("[ref] CTRL+C to stop", flush=True)
    try:
        while frame_budget is None or frame_budget > 0:
            if frame_budget is not None:
                frame_budget -= 1
            raw = get_radar_data(
                my_sdr=my_sdr,
                my_phaser=my_phaser,
                start_offset_samples=start_offset_samples,
                num_bursts=num_chirps,
                n_frame=n_frame,
                good_ramp_samples=good_ramp_samples,
                arm_delay_s=args.tdd_arm_delay_s,
            )
            if args.save_data:
                saved_bursts.append(raw.copy())
            processed = apply_mti(raw) if args.mti else raw
            radar_data = freq_process(
                processed, min_scale=args.min_scale, max_scale=args.max_scale
            )
            if im is not None:
                im.set_data(radar_data)
                plt.show(block=False)
                plt.pause(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            my_sdr.tx_destroy_buffer()
        except Exception:
            pass
        print("[ref] Pluto buffer cleared")
        if args.save_data and saved_bursts:
            np.save(args.save_path, np.asarray(saved_bursts, dtype=np.complex64))
            cfg = np.array(
                [
                    sample_rate,
                    signal_freq,
                    output_freq,
                    num_chirps,
                    chirp_bw,
                    ramp_time_s,
                    tdd.frame_length_ms,
                ],
                dtype=float,
            )
            np.save(args.save_path.with_name(args.save_path.stem + "_config.npy"), cfg)
            print(f"[ref] Saved {len(saved_bursts)} bursts -> {args.save_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
