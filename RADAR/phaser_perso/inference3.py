"""
FMCW-style micro-Doppler extraction:
- Capture a block of samples arranged as [n_chirps, n_samples_per_chirp]
- Range FFT on fast-time
- Pick the strongest range bin (or a chosen bin)
- STFT on the slow-time sequence of that bin to get micro-Doppler

NOTE: This assumes your TX is sending FMCW chirps with the same n_samples_per_chirp
and timing. With the current CW setup you won't get meaningful range separation;
use this when you enable chirp TX on the phaser/Pluto.
"""
import os
import time
from datetime import datetime

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import signal

import adi

# URIs
PHASER_URI = "ip:phaser.local"
PLUTO_URI = "ip:phaser.local:50901"

OUT_DIR = "live_specs_fmcw"

# Acquisition config
N_CHIRPS = 32
N_SAMPLES_PER_CHIRP = 256       # fast-time samples per chirp
SAMPLE_RATE = 200_000            # match your chirp sampling rate
INTERVAL = 1.0                    # seconds between captures

# STFT for micro-Doppler (slow-time)
NPERSEG = 64
NOVERLAP = 48

# TX/RX (placeholder—replace with your chirp setup)
TX_FREQ = int(2.4e9)
RX_FREQ = TX_FREQ
TX_GAIN = -10
RX_GAIN = 30


def acquire_block(sdr, total_samples):
    data = sdr.rx()
    if isinstance(data, (list, tuple)):
        data = data[0]
    return np.asarray(data[:total_samples], dtype=np.complex64)


def range_fft(data_2d):
    # data_2d: [n_chirps, n_samples_per_chirp]
    window = np.hanning(data_2d.shape[1])
    rng_fft = np.fft.fft(data_2d * window, axis=1)
    return rng_fft


def pick_bin(rng_fft, bin_idx=None):
    # pick strongest bin if none provided
    if bin_idx is None:
        mags = np.abs(rng_fft).mean(axis=0)
        bin_idx = int(np.argmax(mags))
    return rng_fft[:, bin_idx], bin_idx


def make_micro_doppler(bin_series, prf):
    f, t, Pxx = signal.spectrogram(
        bin_series,
        fs=prf,
        window="hann",
        nperseg=NPERSEG,
        noverlap=NOVERLAP,
        detrend=False,
        scaling="spectrum",
        mode="psd",
    )
    Sxx = 10 * np.log10(Pxx + 1e-12)
    Sxx = np.flipud(Sxx)
    return Sxx


def save_img(Sxx):
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(OUT_DIR, f"fmcw_md_{ts}.png")
    plt.figure(figsize=(4, 4))
    plt.imshow(Sxx, aspect="auto", cmap="jet", origin="lower")
    plt.axis("off")
    plt.tight_layout(pad=0)
    plt.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0)
    plt.close()
    return path


def main():
    total = N_CHIRPS * N_SAMPLES_PER_CHIRP
    prf = SAMPLE_RATE / N_SAMPLES_PER_CHIRP  # chirp repetition rate

    phaser = adi.CN0566(uri=PHASER_URI)
    sdr = adi.ad9361(uri=PLUTO_URI)
    sdr.rx_enabled_channels = [0]
    sdr._rxadc.set_kernel_buffers_count(1)
    sdr.rx_buffer_size = total  # not *2

    sdr.gain_control_mode = "manual"
    sdr.rx_hardwaregain = RX_GAIN
    sdr.rx_lo = RX_FREQ

    # TODO: set up FMCW TX chirp here. Currently just carrier; range FFT will not be meaningful without chirp.
    sdr.tx_enabled_channels = [0]
    sdr.tx_lo = TX_FREQ
    sdr.tx_cyclic_buffer = True
    tone = np.ones(512, dtype=np.complex64)
    sdr.tx(tone)
    sdr.tx_hardwaregain = TX_GAIN

    try:
        while True:
            raw = acquire_block(sdr, total)
            if raw.size < total:
                print("Short read, skipping")
                continue
            data_2d = raw.reshape(N_CHIRPS, N_SAMPLES_PER_CHIRP)
            rng_fft = range_fft(data_2d)
            bin_series, idx = pick_bin(rng_fft, bin_idx=None)
            md = make_micro_doppler(bin_series, prf=prf)
            path = save_img(md)
            print(f"Saved {path} (range bin {idx}, PRF {prf:.1f} Hz)")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass


if __name__ == "__main__":
    main()

