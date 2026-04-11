import os
import time
from datetime import datetime
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import adi

# Hard-coded URIs and simple CW TX/RX setup, mirroring the phaser examples.
PHASER_URI = "ip:phaser.local"      # phaser front-end
PLUTO_URI = "ip:192.168.2.1"        # Pluto/AD9361 (adjust if different)

OUT_DIR = "live_specs"
N_SAMPLES = 4096          # per capture block
INTERVAL = 0.5            # seconds between captures
NPERSEG = 256
NOVERLAP = 200

# TX tone settings
TX_FREQ = int(2.4e9)      # TX LO (adjust to your radar CW freq)
TX_GAIN = -10             # dB (adjust as in working examples)
RX_FREQ = TX_FREQ         # RX LO matched
RX_GAIN = 20              # dB (adjust as needed)


def make_tone(length: int, fs: float, tone_hz: float = 100e3):
    t = np.arange(length) / fs
    return np.exp(2j * np.pi * tone_hz * t).astype(np.complex64)


def acquire_block(sdr, n_samples: int) -> np.ndarray:
    data = sdr.rx()
    if isinstance(data, (list, tuple)):
        data = data[0]
    return np.asarray(data[:n_samples], dtype=np.complex64)


def make_spectrogram(x: np.ndarray):
    Pxx, freqs, bins = plt.mlab.specgram(
        x,
        NFFT=NPERSEG,
        Fs=1.0,
        noverlap=NOVERLAP,
        window=np.hanning(NPERSEG),
        mode="psd",
    )
    Sxx = 10 * np.log10(Pxx + 1e-12)
    Sxx = np.flipud(Sxx)
    return Sxx


def save_spectrogram(Sxx: np.ndarray, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"spec_{ts}.png")
    plt.figure(figsize=(4, 4))
    plt.imshow(Sxx, aspect="auto", cmap="viridis", origin="lower")
    plt.axis("off")
    plt.tight_layout(pad=0)
    plt.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0)
    plt.close()
    return path


def main():
    # Phaser front-end (for beam control; not strictly required for rx())
    phaser = adi.CN0566(uri=PHASER_URI)

    # Pluto/AD9361 for TX/RX
    sdr = adi.ad9361(uri=PLUTO_URI)

    # RX setup
    sdr.rx_enabled_channels = [0]
    sdr.sample_rate = int(1e6)  # adjust to your working example value
    sdr.rx_buffer_size = N_SAMPLES * 2
    sdr.rx_lo = RX_FREQ
    sdr.gain_control_mode = "manual"
    sdr.rx_hardwaregain = RX_GAIN

    # TX setup (CW tone)
    sdr.tx_enabled_channels = [0]
    sdr.tx_lo = TX_FREQ
    sdr.tx_cyclic_buffer = True
    # Create a modest-length tone buffer
    tone = make_tone(1024, fs=sdr.sample_rate, tone_hz=100e3)
    sdr.tx(tone)
    sdr.tx_hardwaregain = TX_GAIN

    print(f"TX on {TX_FREQ/1e6:.1f} MHz, RX on {RX_FREQ/1e6:.1f} MHz")

    try:
        while True:
            raw = acquire_block(sdr, N_SAMPLES)
            Sxx = make_spectrogram(raw)
            path = save_spectrogram(Sxx, OUT_DIR)
            print(f"[{datetime.now().isoformat(timespec='seconds')}] saved {path}")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("Stopping acquisition.")
    finally:
        # Stop TX
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass


if __name__ == "__main__":
    main()

