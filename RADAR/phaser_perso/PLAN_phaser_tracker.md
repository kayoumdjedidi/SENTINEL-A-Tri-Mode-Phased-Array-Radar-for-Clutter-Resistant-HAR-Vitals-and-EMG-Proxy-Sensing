# SENTINEL Mode A -- Radar Foundation Script Plan

## Objective

Build `sentinel_mode_a.py`: the hardware foundation for Team SENTINEL's AESS RADAR competition entry. This script implements **Mode A (FMCW)** from the SENTINEL proposal with parameters, architecture, and data output aligned for the full tri-mode pipeline.

1. Calibrate with HB100 reference source
2. Run real-time FMCW range-Doppler with MTI + CFAR
3. Generate micro-Doppler spectrograms (the direct input to the RadMamba classifier)
4. Record structured data for the AIRHAR/RadMamba ML pipeline
5. Expose per-element Rx channels for future MVDR/Capon nulling

Runs from WSL over iiod network bridge. No TDD engine.

---

## Alignment with SENTINEL Proposal

From the SENTINEL PDF, Mode A spec:

> "Chirp Bandwidth: 200 MHz -> ~0.75 m range resolution, sufficient to separate human torso vs. limb signatures"
> "Ramp Duration: 200 us, Repetition Frequency: 5 kHz"

| SENTINEL Spec | This Script | Status |
|---|---|---|
| FMCW tracking + Doppler | Range-Doppler + manual beam steering | Core, implemented |
| BW = 200 MHz, ramp = 200 us, PRF = 5 kHz | Matched exactly | Aligned |
| Adaptive beamforming (MVDR/Capon) | Not implemented; logger preserves Pluto channels only | Phase 2 research |
| Virtual arrays (dual TX toggle) | Not yet (single TX) | Phase 3 |
| Raw receive channels preserved | Pluto RX0/RX1 saved separately for offline experiments | Ready |
| STFT micro-Doppler spectrograms | Accumulated in GUI + saved to disk | Aligned |
| Structured data for ML pipeline | Session folder with `raw_bursts/raw_ch0/raw_ch1` + spectrogram/meta files | Aligned |
| Mode scheduling (FMCW + CW interleave) | FMCW only, clean mode-switch API | Phase 2 |
| Tiny Mamba classifier on edge | Not in this script (separate AIRHAR repo) | Separate concern |

---

## Hardware Assumed

- CN0566 Phaser: 8-element ADAR1000, ADF4159 PLL, LTC5548 mixers
- ADALM-PLUTO (AD9361): 2.1 GHz IF, via Pi iiod bridge (port 50901)
- Vivaldi antenna on TX_OUT_2
- HB100 source (~10.525 GHz) for calibration
- Pi running iiod at phaser.local

---

## Architecture Decisions

### 1. No TDD -- continuous_sawtooth with delay_word = 0

**Problem:** TDD `single_sawtooth_burst` requires sub-ms GPIO trigger synchronization. Over iiod network from WSL, this fails with `TimeoutError` + segfault (verified).

**Solution:** `continuous_sawtooth` mode. The ADF4159 free-runs with chirp period = ramp_time. In continuous sawtooth, the fast retrace takes <1 PFD cycle (~10 ns), so with `delay_word = 0` the actual period matches `freq_dev_time` to within 0.001%. Over 256 chirps the cumulative drift is ~2.5 samples -- negligible.

One large `rx()` call grabs `num_chirps + 1` chirps. We skip the first frame (may start mid-chirp) and segment the rest by N_frame.

**Frequency naming rule:** in this SENTINEL / range-Doppler path, `output_freq` means the actual radiated RF center. The code must always compute the synthesizer setting as `vco_freq = output_freq + signal_freq + center_freq`. Do not copy the older waterfall-script convention where `output_freq` was used for the ADF4159 LO itself.

### 2. PyQt5 + pyqtgraph, QThread rx worker

Proven in our FMCW_RADAR_Waterfall.py fix. 5-10x faster than matplotlib for real-time image updates. QThread + pyqtSignal for non-blocking `rx()`.

### 3. Raw Pluto Rx channels preserved

The script stores `data[0]` and `data[1]` separately in the raw recording. For display, we sum them (standard for the current analog-beamformed path). These are preserved for offline experiments and debugging, but they are **not** the full per-element ADAR1000 channels.

### 4. Micro-Doppler spectrogram accumulation

Each frame produces a range-Doppler map. We extract the Doppler profile at a **selected range gate** (user-configurable or auto-tracked via CFAR peak). These Doppler columns stack into a time-frequency spectrogram -- the exact input to the RadMamba classifier. The spectrogram scrolls in the GUI and is saved with each recording session.

### 5. Gain taper: Chebyshev [8, 34, 84, 127, 127, 84, 34, 8]

SENTINEL proposal specifies "Chebyshev taper baseline." The ADAR1000 8-element Chebyshev weights for -30 dB sidelobes are approximately [8, 34, 84, 127, 127, 84, 34, 8] (same values as Jon Kraft's "Blackman" taper). Provides ~13 dB sidelobe suppression vs uniform.

---

## Parameters (SENTINEL Mode A aligned)

| Parameter | Value | Rationale |
|---|---|---|
| sample_rate | 4 MHz | Max reliable over iiod network |
| center_freq | 2.1 GHz | Standard Pluto IF |
| signal_freq | 100 kHz | Standard IF offset |
| output_freq | 10.25 GHz | X-band center (10.0-10.5 GHz allocation) |
| chirp_BW | 200 MHz | SENTINEL spec: 0.75 m range resolution |
| ramp_time | 200 us | SENTINEL spec: 5 kHz PRF |
| num_chirps | 256 | Good Doppler resolution for micro-Doppler HAR |
| rx_gain | 60 dB | Max sensitivity for small targets |
| tx_gain | 0 dB | Max TX power |
| gain_taper | Chebyshev -30 dB | [8, 34, 84, 127, 127, 84, 34, 8] |
| delay_word | 0 | Period = ramp_time exactly |
| ramp_mode | continuous_sawtooth | No TDD needed |

### Derived Performance

| Metric | Value | Formula |
|---|---|---|
| Slope | 1 THz/s | 200 MHz / 200 us |
| Range resolution | 0.75 m | c / (2 * 200 MHz) |
| Max displayable range | ~285 m | (fs/2 - f_sig) * c / (2 * slope) |
| PRF | 5000 Hz | 1 / 200 us |
| N_frame | 800 samples | 200 us * 4 MHz |
| Velocity resolution | 0.29 m/s | lambda / (2 * 256 * 200 us) |
| Max unambiguous velocity | +/- 36.6 m/s (132 km/h) | PRF/2 * lambda/2 |
| Wavelength | 29.3 mm | c / 10.25 GHz |
| Buffer size | 2^18 = 262144 | covers 257+ chirps (328 actual) |
| CPI | 51.2 ms | 256 * 200 us |
| Expected frame rate | ~12-15 FPS | limited by network + processing |

The 5 kHz PRF gives +/- 36.6 m/s max velocity -- covers walking (1.5 m/s), running (5 m/s), drone flight (15 m/s), and even cars in parking lots. The 0.29 m/s velocity resolution is sufficient to see limb micro-Doppler signatures for HAR.

Because the WSL -> Pi -> Pluto bridge can occasionally return slightly short buffers, segmentation must tolerate missing tail samples. The runtime therefore zero-pads incomplete frames and records `valid_chirps.npy` so offline tooling can ignore padded chirps if needed.

### BW slider range

BW is adjustable 100-500 MHz via GUI slider (SENTINEL spec = 200 MHz default, but 500 MHz available for fine-range experiments). When BW changes:
- `freq_dev_range` updates on the ADF4159
- Range axis, slope, and R_res recalculate
- ramp_time stays at 200 us (PRF stays at 5 kHz)

---

## Execution Flow

### Phase 0: Startup & Hardware Init

```
1. Connect to Pluto (ip:phaser.local:50901) and CN0566 (ip:phaser.local)
2. set_timeout(0)
3. ADAR1000: Chebyshev taper, all phases = 0
4. GPIOs: tx_sw=0, vctrl_1=1, vctrl_2=1
5. try: tx_destroy_buffer()  # clear stale DMA
6. Configure SDR: sample_rate, rx_lo, tx_lo, gains, channels [0,1]
```

### Phase 1: HB100 Calibration (optional, --skip-cal to bypass)

```
1. ramp_mode = "disabled" (CW, no chirp)
2. output_freq = 10.525 GHz (HB100 nominal)
   vco_freq = 10.525e9 + 100e3 + 2.1e9 = 12.6251 GHz
   my_phaser.frequency = int(vco_freq / 4)
3. Generate CW tone at signal_freq, start TX
4. Capture 20 buffers (FFT = 65536), average power spectra
5. Peak search within +/- 200 kHz of signal_freq
6. SNR > 15 dB above median: PASS, print/store freq offset
7. Else: WARN, continue
8. Destroy TX buffer, flush Rx
```

### Phase 2: FMCW Range-Doppler (main mode)

```
1. Configure ADF4159: continuous_sawtooth, BW=200MHz, ramp=200us, delay_word=0
2. Generate sinewave at signal_freq, start TX
3. Launch GUI
4. Start QThread rx worker:
   - rx() -> emit raw [data[0], data[1]] via pyqtSignal
5. GUI update slot processes and renders
```

---

## Signal Processing Pipeline (per frame)

### Step 1: Chirp Segmentation
```python
ch0, ch1 = data[0], data[1]
sum_data = ch0 + ch1                       # analog beamformer sum
rx_bursts = np.zeros((N_chirps, N_frame), dtype=complex)
offset = N_frame                           # skip first (possibly partial) chirp
for i in range(N_chirps):
    rx_bursts[i] = sum_data[offset + i*N_frame : offset + (i+1)*N_frame]
```

### Step 2: Windowing
```python
win = np.blackman(N_frame)
rx_bursts *= win[np.newaxis, :]            # vectorized, no loop
```

### Step 3: MTI 2-Pulse Canceller (toggleable)
```python
filtered = np.zeros_like(rx_bursts)
for i in range(N_chirps - 1):
    corr = np.correlate(rx_bursts[i], rx_bursts[i+1], 'valid')
    phi = np.angle(corr[0])
    filtered[i] = rx_bursts[i+1] - rx_bursts[i] * np.exp(-1j * phi)
```

### Step 4: 2D FFT -> Range-Doppler Map
```python
rd = np.fft.fftshift(np.abs(np.fft.fft2(filtered)))
rd_db = 20 * np.log10(rd + 1e-10)
```
Shape: (N_chirps, N_frame). After `.T`: (N_frame, N_chirps) for display.
- Rows = range bins (y-axis, 0 to max_range)
- Columns = Doppler bins (x-axis, -max_vel to +max_vel)

### Step 5: Zero-Range Suppression
```python
center = N_frame // 2
rd_db[center - 5 : center + 5, :] = rd_db.min()
```

### Step 6: CFAR on Range Profile
```python
range_profile = rd_db.max(axis=1)   # peak Doppler per range bin
cfar_thresh, detections = cfar(range_profile, guard=15, ref=16, bias=25)
```

### Step 7: Micro-Doppler Spectrogram Extraction
```python
# Auto-select range gate: CFAR peak, or manual slider
target_range_bin = np.argmax(range_profile[center+5:]) + center + 5
doppler_column = rd_db[target_range_bin, :]  # shape: (N_chirps,)
spectrogram.roll_and_append(doppler_column)  # scrolling STFT image
```

This spectrogram is the direct input feature for RadMamba.

---

## GUI Layout

```
+--------------------------------------------------------------------+
|            SENTINEL Mode A -- FMCW Radar                           |
+----------+------------------+--------------------------------------+
| Controls | Range Profile    | Range-Doppler Heatmap                |
|          | (1D + CFAR line) | (range vs velocity, inferno cmap)    |
| Steer    |                  |                                      |
| [-80..80]|  dB    CFAR      |  y: 0..60 m (adjustable)             |
|          |  |     thresh    |  x: -10..+10 m/s                     |
| BW       |  |      |       |                                      |
| [100-500]|  |      |       |  [target marker if CFAR detection]    |
|          |  +------+------->|                                      |
| MTI  [x] |      range (m)  |                                      |
| CFAR [x] |                  |                                      |
| bias [25]+------------------+--------------------------------------+
|          | Micro-Doppler Spectrogram                                |
| Range    | (Doppler vs time at selected range gate)                 |
| Gate [m] |                                                         |
|          |  y: velocity     |  scrolling time axis                  |
| Save [x] |  x: time (s)    |  << this feeds RadMamba >>           |
|          |                                                         |
| [QUIT]   +---------------------------------------------------------+
+--------------------------------------------------------------------+
```

### Controls
- **Steering angle**: -80 to +80 deg. `my_phaser.set_beam_phase_diff()`
- **Chirp BW**: 100-500 MHz (default 200 = SENTINEL spec). Updates ADF4159 live.
- **MTI**: on/off checkbox
- **CFAR**: on/off + bias slider (0-100)
- **Range gate**: manual slider or "auto" (follows CFAR peak)
- **Save**: toggle recording (raw data + micro-Doppler spectrograms)
- **Quit**: destroy TX buffer, save any pending data, close

---

## Data Recording Format (ML-pipeline aligned)

When Save is enabled, every frame appends to a session file:

```
recordings/
  session_YYYYMMDD_HHMMSS/
    raw_bursts.npy        # shape: [n_frames, n_chirps, n_samples] complex64 (summed, unwindowed)
    raw_ch0.npy           # shape: [n_frames, n_chirps, n_samples] complex64 (Pluto RX0)
    raw_ch1.npy           # shape: [n_frames, n_chirps, n_samples] complex64 (Pluto RX1)
    spectrograms.npy      # shape: [n_frames, n_doppler_bins] float32
    valid_chirps.npy      # shape: [n_frames] int32, full chirps present before zero-padding
    timestamps.npy        # shape: [n_frames] float64, unix time
    config.json           # all parameters + calibration offset
    labels.json           # placeholder for ground-truth annotation
```

This maps directly to the AIRHAR integration plan:
> "Store under something like `recordings/cn0566/subject_X/session_Y/{raw.npy, labels.json}`"

The `prepare_cn0566.py` script in the AIRHAR repo reads these files, applies STFT, and produces training tensors.

---

## Extensibility Hookpoints (for SENTINEL Phases 2-3)

### Mode B (CW vitals) hookpoint
The script's calibration phase already demonstrates CW mode switching (`ramp_mode = "disabled"`). To interleave CW dwells with FMCW bursts:
1. After N FMCW frames, switch `ramp_mode = "disabled"`, hold for CW dwell (2-8 s)
2. Capture long-integration CW data for HR/RR extraction
3. Switch back to `continuous_sawtooth`
Guard time between modes prevents self-interference.

### MVDR/Capon nulling hookpoint
This script does **not** yet expose true per-element array data, so MVDR/Capon is not a drop-in next step from the saved files alone. The practical hookpoint is:
1. Use this script as the stable Mode A logger/visualizer.
2. Add a separate acquisition path if you need true element-space or virtual-array data.
3. Build nulling experiments on top of that richer acquisition mode, not on the current summed-beam runtime.

### Virtual array hookpoint
Toggle `my_phaser._gpios.gpio_tx_sw` between 0 and 1 every N chirps. This doubles the effective aperture for AoA estimation. Requires interleaved TX and per-TX calibration.

---

## Verification Steps

### 1. HB100 Calibration Check
- Power on HB100, point at antenna from ~1 m
- Run calibration phase
- Expect: peak near 100 kHz with >20 dB SNR
- Record: exact peak frequency, SNR, signal level

### 2. Static Aluminum Sheet (3-5 m)
- MTI OFF: bright peak at correct range + TX leakage at 0 m
- MTI ON: sheet disappears (stationary), leakage suppressed
- CFAR ON: threshold line visible, no false detections

### 3. Moving Aluminum Sheet
- MTI ON: peak appears in range-Doppler at correct range
- Velocity axis shows motion direction (+ = away, - = toward)
- CFAR highlights the moving target
- Micro-Doppler panel shows velocity trace over time

### 4. Human Subject (walking, arm swings)
- Range-Doppler: torso at one range, limb micro-Doppler spread around it
- Micro-Doppler spectrogram: characteristic walking signature (sinusoidal limb pattern)
- This validates the data quality for RadMamba training

---

## Files

```
workspace/radar/phaser_perso/
    sentinel_mode_a.py             <- main script (this plan)
    target_detection_dbfs.py       <- CFAR utility (from reference/)
    Range_Doppler_NoTDD.py         <- simpler predecessor (kept as fallback)
    PLAN_phaser_tracker.md         <- this plan
    reference/                     <- Jon Kraft's original scripts
    recordings/                    <- session data (created at runtime)
```

---

## Risk Mitigations

| Risk | Mitigation |
|---|---|
| TX buffer busy from crashed run | `try: tx_destroy_buffer()` before `tx()` |
| rx() timeout over network | `set_timeout(0)` for infinite timeout |
| Chirp misalignment (no TDD) | Skip first N_frame; zero-pad short tails; record `valid_chirps.npy` |
| Phase drift across chirps | <2.5 samples drift over 256 chirps (negligible) |
| BW change crashes PLL | Only update `freq_dev_range` + `enable = 0` (latch) |
| Spectrogram jitter | Fixed frame timing via QThread; timestamp each frame |
| Large data files | Compress to complex64 (not complex128); ~50 MB per 60 s session |
