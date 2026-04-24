#!/usr/bin/env python3
"""Reference-inspired continuous FMCW with per-frame chirp-boundary search.

Why this exists
---------------
The ADI reference range-Doppler path depends on TDD-synchronised burst capture.
On the forwarded `phaser.local:50901` path that burst capture times out at
`rx()`, so the direct reference chain is not available from this environment.

This script takes the next first-principles step:
- keep the hardware close to the reference FMCW settings
- use continuous sawtooth instead of burst TDD
- estimate the chirp start offset from each raw capture instead of assuming it
- then build the chirp matrix and apply the same core 2D FFT / optional MTI

It is intentionally simple: one beat-FFT pane, one range-Doppler pane, a few
diagnostic prints, and no gating/background/UI extras.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

plt.close("all")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinel_modeA_ref_contsync",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rpi-uri", default="ip:phaser.local")
    p.add_argument("--sdr-uri", default="ip:phaser.local:50901")
    p.add_argument("--sample-rate", type=float, default=0.6e6)
    p.add_argument("--center-freq", type=float, default=2.1e9)
    p.add_argument("--signal-freq", type=float, default=100e3)
    p.add_argument("--rx-gain", type=int, default=70)
    p.add_argument("--tx-gain", type=int, default=0)
    p.add_argument("--output-freq", type=float, default=10.25e9)
    p.add_argument("--bw", type=float, default=500e6)
    p.add_argument("--ramp", type=int, default=500)
    p.add_argument("--chirps", type=int, default=128)
    p.add_argument("--max-range", type=float, default=10.0)
    p.add_argument("--min-scale", type=float, default=0.0)
    p.add_argument("--max-scale", type=float, default=8.0)
    p.add_argument("--mti", action="store_true")
    p.add_argument(
        "--good-start-frac",
        type=float,
        default=0.1,
        help="Ignore the first fraction of each chirp when building the good ramp",
    )
    p.add_argument(
        "--good-len-frac",
        type=float,
        default=0.85,
        help="Fraction of each chirp used after the start skip",
    )
    p.add_argument(
        "--spc-search",
        type=int,
        default=3,
        help="Search +/- this many samples around the nominal samples-per-chirp",
    )
    p.add_argument(
        "--save-data",
        action="store_true",
        help="Save synchronised chirp matrices for offline inspection",
    )
    p.add_argument(
        "--save-path",
        type=Path,
        default=Path("contsync_capture.npy"),
        help="Save path when --save-data is enabled",
    )
    return p


def next_power_of_two(n: int) -> int:
    return 1 << math.ceil(math.log2(max(1, n)))


def apply_mti(rx_bursts: np.ndarray) -> np.ndarray:
    chirps = np.asarray(rx_bursts, dtype=np.complex64)
    out = np.zeros_like(chirps)
    for chirp in range(chirps.shape[0] - 1):
        c0 = chirps[chirp, :]
        c1 = chirps[chirp + 1, :]
        corr = np.correlate(c0, c1, "valid")
        angle_diff = np.angle(corr[0]) if corr.size else 0.0
        out[chirp, :] = c1 - c0 * np.exp(-1j * angle_diff)
    return out


def freq_process(data: np.ndarray, *, min_scale: float, max_scale: float) -> np.ndarray:
    rd = np.fft.fftshift(np.abs(np.fft.fft2(data)))
    rd = np.log10(np.maximum(rd, 1e-12)).T
    return np.clip(rd, min_scale, max_scale)


def beat_profile_db(chirps: np.ndarray) -> np.ndarray:
    mean_chirp = np.asarray(chirps, dtype=np.complex64).mean(axis=0)
    window = np.blackman(mean_chirp.size)
    spec = np.fft.fftshift(np.fft.fft(mean_chirp * window))
    mag = np.abs(spec) / max(np.sum(window), 1.0)
    return 20.0 * np.log10(np.maximum(mag, 1e-15) / 2.0**11)


def coherence_metric(raw: np.ndarray, *, offset: int, spc: int, num_chirps: int, good_start: int, good_len: int) -> float:
    last_needed = offset + (num_chirps - 1) * spc + good_start + good_len
    if last_needed > raw.size or good_len <= 8:
        return -1.0

    score = 0.0
    count = 0
    for chirp_idx in range(num_chirps - 1):
        s0 = offset + chirp_idx * spc + good_start
        s1 = offset + (chirp_idx + 1) * spc + good_start
        c0 = raw[s0:s0 + good_len]
        c1 = raw[s1:s1 + good_len]
        n0 = float(np.linalg.norm(c0))
        n1 = float(np.linalg.norm(c1))
        if n0 <= 1e-9 or n1 <= 1e-9:
            continue
        score += float(np.abs(np.vdot(c0, c1)) / (n0 * n1))
        count += 1
    return score / count if count else -1.0


def estimate_segmentation(
    raw: np.ndarray,
    *,
    nominal_spc: int,
    spc_search: int,
    num_chirps: int,
    good_start_frac: float,
    good_len_frac: float,
) -> tuple[int, int, int, float]:
    best_score = -1.0
    best_spc = nominal_spc
    best_offset = 0
    best_good_len = max(16, int(nominal_spc * good_len_frac))

    for spc in range(max(32, nominal_spc - spc_search), nominal_spc + spc_search + 1):
        good_start = int(round(spc * good_start_frac))
        good_len = int(round(spc * good_len_frac))
        good_len = min(good_len, spc - good_start - 1)
        if good_len < 16:
            continue
        for offset in range(spc):
            score = coherence_metric(
                raw,
                offset=offset,
                spc=spc,
                num_chirps=num_chirps,
                good_start=good_start,
                good_len=good_len,
            )
            if score > best_score:
                best_score = score
                best_spc = spc
                best_offset = offset
                best_good_len = good_len
    return best_offset, best_spc, int(round(best_spc * good_start_frac)), best_score


def slice_chirps(
    raw: np.ndarray,
    *,
    offset: int,
    spc: int,
    num_chirps: int,
    good_start: int,
    good_len: int,
) -> np.ndarray:
    chirps = np.zeros((num_chirps, good_len), dtype=np.complex64)
    for chirp_idx in range(num_chirps):
        start = offset + chirp_idx * spc + good_start
        stop = start + good_len
        chirps[chirp_idx, :] = raw[start:stop]
    return chirps


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import adi

    print(adi.__version__)

    sample_rate = float(args.sample_rate)
    center_freq = float(args.center_freq)
    signal_freq = float(args.signal_freq)
    rx_gain = int(args.rx_gain)
    tx_gain = int(args.tx_gain)
    output_freq = float(args.output_freq)
    chirp_bw = float(args.bw)
    ramp_time = int(args.ramp)
    num_chirps = int(args.chirps)

    my_sdr = adi.ad9361(uri=args.sdr_uri)
    my_sdr._ctx.set_timeout(5000)
    my_phaser = adi.CN0566(uri=args.rpi_uri, sdr=my_sdr)

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
    my_phaser.delay_word = 0
    my_phaser.delay_clk = "PFD"
    my_phaser.delay_start_en = 0
    my_phaser.ramp_delay_en = 0
    my_phaser.trig_delay_en = 0
    my_phaser.ramp_mode = "continuous_sawtooth"
    my_phaser.sing_ful_tri = 0
    my_phaser.tx_trig_en = 0
    my_phaser.enable = 0

    n = 2 ** 18
    ts = 1.0 / sample_rate
    t = np.arange(0, n * ts, ts)
    i = np.cos(2 * np.pi * t * signal_freq) * 2**14
    q = np.sin(2 * np.pi * t * signal_freq) * 2**14
    iq = (0.9 * (i + 1j * q)).astype(np.complex64)
    my_sdr.tx([iq, iq])

    ramp_time_s = int(my_phaser.freq_dev_time) / 1e6
    nominal_spc = int(round(ramp_time_s * sample_rate))
    raw_spc = ramp_time_s * sample_rate
    buf_target = (num_chirps + 4) * max(1, nominal_spc + args.spc_search)
    my_sdr.rx_buffer_size = next_power_of_two(buf_target)

    c = 3e8
    wavelength = c / output_freq
    chirp_dt = nominal_spc / sample_rate
    prf = 1.0 / chirp_dt
    max_doppler_vel = (prf / 2.0) * wavelength / 2.0
    r_res = c / (2 * chirp_bw)
    v_res = wavelength / (2 * num_chirps * chirp_dt)

    print(
        f"[contsync] rf={output_freq/1e9:.4f} GHz  bw={chirp_bw/1e6:.0f} MHz  "
        f"ramp={int(ramp_time_s*1e6)} us  chirps={num_chirps}\n"
        f"[contsync] raw_spc={raw_spc:.3f}  nominal_spc={nominal_spc}  "
        f"rx_buffer_size={my_sdr.rx_buffer_size}\n"
        f"[contsync] R_res={r_res:.3f} m  v_res≈{v_res:.3f} m/s"
    )

    raw = my_sdr.rx()
    raw_sum = np.asarray(raw[0], dtype=np.complex64) + np.asarray(raw[1], dtype=np.complex64)
    best_offset, best_spc, good_start, best_score = estimate_segmentation(
        raw_sum,
        nominal_spc=nominal_spc,
        spc_search=args.spc_search,
        num_chirps=num_chirps,
        good_start_frac=args.good_start_frac,
        good_len_frac=args.good_len_frac,
    )
    good_len = min(int(round(best_spc * args.good_len_frac)), best_spc - good_start - 1)
    chirps = slice_chirps(
        raw_sum,
        offset=best_offset,
        spc=best_spc,
        num_chirps=num_chirps,
        good_start=good_start,
        good_len=good_len,
    )
    proc = apply_mti(chirps) if args.mti else chirps
    beat_db = beat_profile_db(chirps)
    radar_data = freq_process(proc, min_scale=args.min_scale, max_scale=args.max_scale)

    slope = chirp_bw / ramp_time_s
    freq_axis = np.linspace(-sample_rate / 2, sample_rate / 2, good_len)
    dist = (freq_axis - signal_freq) * c / (2 * slope)
    extent = [-max_doppler_vel, max_doppler_vel, dist.min(), dist.max()]

    fig, (ax_fft, ax_rd) = plt.subplots(1, 2, figsize=(15, 6))
    fft_curve, = ax_fft.plot(dist, beat_db, color="gold", linewidth=1.2)
    ax_fft.set_title("Beat-Freq FFT")
    ax_fft.set_xlabel("Range [m]")
    ax_fft.set_ylabel("dBFS")
    ax_fft.set_xlim(0, args.max_range)
    ax_fft.set_ylim(-100, 0)
    ax_fft.grid(True, alpha=0.2)

    im = ax_rd.imshow(
        radar_data,
        aspect="auto",
        extent=extent,
        origin="lower",
        cmap=matplotlib.colormaps.get_cmap("inferno"),
    )
    ax_rd.set_title("Continuous FMCW Range-Doppler")
    ax_rd.set_xlabel("Velocity [m/s]")
    ax_rd.set_ylabel("Range [m]")
    ax_rd.set_xlim([-10, 10])
    ax_rd.set_ylim([0, args.max_range])

    saved: list[np.ndarray] = []
    print("[contsync] CTRL+C to stop")
    try:
        frame = 0
        while True:
            raw = my_sdr.rx()
            raw_sum = np.asarray(raw[0], dtype=np.complex64) + np.asarray(raw[1], dtype=np.complex64)
            best_offset, best_spc, good_start, best_score = estimate_segmentation(
                raw_sum,
                nominal_spc=nominal_spc,
                spc_search=args.spc_search,
                num_chirps=num_chirps,
                good_start_frac=args.good_start_frac,
                good_len_frac=args.good_len_frac,
            )
            good_len = min(int(round(best_spc * args.good_len_frac)), best_spc - good_start - 1)
            chirps = slice_chirps(
                raw_sum,
                offset=best_offset,
                spc=best_spc,
                num_chirps=num_chirps,
                good_start=good_start,
                good_len=good_len,
            )
            if args.save_data:
                saved.append(chirps.copy())
            proc = apply_mti(chirps) if args.mti else chirps
            beat_db = beat_profile_db(chirps)
            radar_data = freq_process(proc, min_scale=args.min_scale, max_scale=args.max_scale)
            fft_curve.set_data(dist, beat_db)
            im.set_data(radar_data)
            plt.show(block=False)
            plt.pause(0.05)
            if frame % 10 == 0:
                print(
                    f"[contsync] frame={frame}  spc={best_spc}  offset={best_offset}  "
                    f"good_start={good_start}  good_len={good_len}  score={best_score:.4f}"
                )
            frame += 1
    except KeyboardInterrupt:
        pass
    finally:
        try:
            my_sdr.tx_destroy_buffer()
        except Exception:
            pass
        if args.save_data and saved:
            np.save(args.save_path, np.asarray(saved, dtype=np.complex64))
            cfg = np.array(
                [
                    sample_rate,
                    signal_freq,
                    output_freq,
                    num_chirps,
                    chirp_bw,
                    ramp_time_s,
                    best_spc / sample_rate * 1e3,
                ],
                dtype=float,
            )
            np.save(args.save_path.with_name(args.save_path.stem + "_config.npy"), cfg)
            print(f"[contsync] saved {len(saved)} frames -> {args.save_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
