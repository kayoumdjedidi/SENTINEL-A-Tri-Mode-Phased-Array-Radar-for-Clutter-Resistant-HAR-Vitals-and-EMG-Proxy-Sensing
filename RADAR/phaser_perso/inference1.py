import os
import time
from datetime import datetime
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import adi

# Hard-coded URIs, matching the phaser examples
PHASER_URI = "ip:phaser.local"
PLUTO_URI = "ip:phaser.local:50901"   # adjust if your Pluto is a different IP or shared context

OUT_DIR = "live_specs"
N_SAMPLES = 4096       # per capture block
INTERVAL = 0.5         # seconds between captures
NPERSEG = 256
NOVERLAP = 200

# TX settings
TX_FREQ = int(2.4e9)   # CW transmit LO (set to your radar freq)
RX_FREQ = TX_FREQ      # RX LO matched to TX
TX_GAIN = -10          # dB, adjust to your known-good value
RX_GAIN = 20           # dB, adjust to your known-good value
TONE_HZ = 100_000      # baseband tone to upconvert; set 0 for pure CW


def acquire_block(sdr, n_samples):
    data = sdr.rx()
    if isinstance(data, (list, tuple)):
        data = data[0]
    return np.asarray(data[:n_samples], dtype=np.complex64)

def make_spectrogram(x):
    Pxx, freqs, bins = plt.mlab.specgram(
        x, NFFT=NPERSEG, Fs=1.0, noverlap=NOVERLAP,
        window=np.hanning(NPERSEG), mode="psd"
    )
    Sxx = 10 * np.log10(Pxx + 1e-12)
    Sxx = np.flipud(Sxx)
    return Sxx

def save_spectrogram(Sxx):
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(OUT_DIR, f"spec_{ts}.png")
    plt.figure(figsize=(4,4))
    plt.imshow(Sxx, aspect="auto", cmap="viridis", origin="lower")
    plt.axis("off")
    plt.tight_layout(pad=0)
    plt.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0)
    plt.close()
    return path

def main():
    # Set up phaser front-end (even if just to match the examples)
    phaser = adi.CN0566(uri=PHASER_URI)

    # Set up Pluto/AD9361 for TX+RX (same pattern as phaser_find_hb100.py)
    sdr = adi.ad9361(uri=PLUTO_URI)
    sdr.rx_enabled_channels = [0]
    sdr.sample_rate = int(1e6)          # adjust to your usual value
    sdr.rx_buffer_size = N_SAMPLES*2    # some headroom
    # RX gains/LO
    sdr.gain_control_mode = "manual"
    sdr.rx_hardwaregain = RX_GAIN
    sdr.rx_lo = RX_FREQ

    # TX setup: simple cyclic tone
    sdr.tx_enabled_channels = [0]
    sdr.tx_lo = TX_FREQ
    sdr.tx_cyclic_buffer = True
    # Generate tone at baseband; set TONE_HZ=0 for pure CW
    t = np.arange(1024) / sdr.sample_rate
    tone = np.exp(2j * np.pi * TONE_HZ * t).astype(np.complex64)
    sdr.tx(tone)
    sdr.tx_hardwaregain = TX_GAIN

    try:
        while True:
            raw = acquire_block(sdr, N_SAMPLES)
            Sxx = make_spectrogram(raw)
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
