import os
import time
from datetime import datetime

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import signal

import adi

# Hard-coded URIs (adjust if needed)
PHASER_URI = "ip:phaser.local"
PLUTO_URI = "ip:phaser.local:50901"

OUT_DIR = "live_specs"
# For ~100 Hz vibration, capture ~1 s worth of data so slow Doppler is observable
SAMPLE_RATE = int(200e3)   # lower Fs to keep buffers reasonable
N_SAMPLES = SAMPLE_RATE    # 1 second of data per capture
INTERVAL = 1.0             # seconds between captures
# STFT settings: balance time/freq resolution for ~100 Hz
NPERSEG = 1024
NOVERLAP = 512

# TX settings
TX_FREQ = int(2.4e9)      # CW transmit LO
RX_FREQ = TX_FREQ         # RX LO matched
TX_GAIN = -10             # dB
RX_GAIN = 30              # dB (adjust if saturating)
TONE_HZ = 0               # set 0 for pure CW

# Slow-time decimation (set to 1 at lower Fs)
DECIM = 1                 # no decimation needed at 200 kHz
# High-pass cutoff after decimation to remove carrier/DC (Hz)
HP_CUT = 5.0


def acquire_block(sdr, n_samples):
    data = sdr.rx()
    if isinstance(data, (list, tuple)):
        data = data[0]
    return np.asarray(data[:n_samples], dtype=np.complex64)


def make_spectrogram(x, fs):
    # Remove DC
    x = x - np.mean(x)
    # Decimate
    if DECIM > 1:
        x = signal.decimate(x, DECIM, ftype="fir", zero_phase=True)
        fs = fs / DECIM
    # High-pass to suppress carrier/leakage
    if HP_CUT > 0 and fs > 2 * HP_CUT:
        b, a = signal.butter(4, HP_CUT / (fs / 2), btype="highpass")
        x = signal.lfilter(b, a, x)
    # Spectrogram on complex baseband
    f, t, Pxx = signal.spectrogram(
        x,
        fs=fs,
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


def save_spectrogram(Sxx):
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(OUT_DIR, f"spec_{ts}.png")
    plt.figure(figsize=(4, 4))
    plt.imshow(Sxx, aspect="auto", cmap="viridis", origin="lower")
    plt.axis("off")
    plt.tight_layout(pad=0)
    plt.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0)
    plt.close()
    return path


def main():
    phaser = adi.CN0566(uri=PHASER_URI)

    sdr = adi.ad9361(uri=PLUTO_URI)
    sdr.rx_enabled_channels = [0]
    sdr.sample_rate = SAMPLE_RATE
    sdr.rx_buffer_size = N_SAMPLES * 2
    sdr.gain_control_mode = "manual"
    sdr.rx_hardwaregain = RX_GAIN
    sdr.rx_lo = RX_FREQ

    sdr.tx_enabled_channels = [0]
    sdr.tx_lo = TX_FREQ
    sdr.tx_cyclic_buffer = True
    t = np.arange(2048) / sdr.sample_rate
    tone = np.exp(2j * np.pi * TONE_HZ * t).astype(np.complex64)
    sdr.tx(tone)
    sdr.tx_hardwaregain = TX_GAIN

    try:
        while True:
            raw = acquire_block(sdr, N_SAMPLES)
            Sxx = make_spectrogram(raw, fs=sdr.sample_rate)
            path = save_spectrogram(Sxx)
            print(f"[{datetime.now().isoformat(timespec='seconds')}] saved {path}")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("Stopping acquisition.")
    finally:
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass


if __name__ == "__main__":
    main()
