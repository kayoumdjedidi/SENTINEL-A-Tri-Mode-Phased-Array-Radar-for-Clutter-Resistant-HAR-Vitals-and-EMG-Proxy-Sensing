# SENTINEL Mode A — Operational Manual

**Hardware:** CN0566 Phaser + AD9361 Pluto SDR  
**Transport:** WSL → Raspberry Pi (`phaser.local`) → Pluto (`50901` IIO context forwarding)  
**Software stack:** `radar_dsp.py` / `radar_backends.py` / `radar_mode_a.py`

---

## Table of Contents

1. [File Map](#1-file-map)
2. [Hardware Chain and What Can Go Wrong](#2-hardware-chain-and-what-can-go-wrong)
3. [Pre-Flight Checks](#3-pre-flight-checks)
4. [Stage 0 — Waterfall Sanity Baseline](#4-stage-0--waterfall-sanity-baseline)
5. [Stage 1 — Offline Replay](#5-stage-1--offline-replay)
6. [Stage 2 — Live Mode A (Continuous Backend)](#6-stage-2--live-mode-a-continuous-backend)
7. [Stage 3 — Live Mode A (TDD Backend)](#7-stage-3--live-mode-a-tdd-backend)
8. [All CLI Flags — `radar_mode_a.py`](#8-all-cli-flags--radar_mode_apy)
9. [All CLI Flags — `offline_replay.py`](#9-all-cli-flags--offline_replaypy)
10. [The Display — What Everything Means](#10-the-display--what-everything-means)
11. [Tuning Ladder](#11-tuning-ladder)
12. [Recording and Replay Workflow](#12-recording-and-replay-workflow)
13. [DSP Reference — What Each Stage Does](#13-dsp-reference--what-each-stage-does)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. File Map

| File | Purpose | Touch hardware? |
|---|---|---|
| `RADAR_FFT_Waterfall_baseline.py` | Stage 0 sanity check. PyQtGraph GUI, continuous triangular ramp, single-channel FFT waterfall. Proven working on this hardware path. Run this first on any new session. | Yes |
| `radar_dsp.py` | Shared DSP library. No hardware, no GUI. `RadarConfig`, MTI, 2D FFT, range-Doppler map, CFAR, range gate, micro-Doppler history, save/load. Import this from any processing script. | No |
| `radar_backends.py` | Two acquisition backends. `TDDBurstBackend` and `ContinuousSawtoothBackend`. Both implement the same `connect()` / `capture()` / `close()` interface. | Yes (live only) |
| `radar_mode_a.py` | Main entry point. Wires a backend to the DSP stack. Live matplotlib GUI. CLI for all tuning and recording options. | Yes (live only) |
| `offline_replay.py` | Load a saved session or raw `.npy` capture and replay it through the DSP stack. Matplotlib display, optional frame output. No hardware needed. | No |
| `reference/Range_Doppler_Plot.py` | ADI golden TDD reference. Read-only. `TDDBurstBackend` is derived directly from this. Do not run directly — use `radar_mode_a.py --acq tdd`. | Yes |
| `reference/FMCW_RADAR_Waterfall.py` | ADI golden waterfall reference. Used as the basis for `RADAR_FFT_Waterfall_baseline.py`. Read-only. | Yes |
| `Range_Doppler_NoTDD.py` | Proven continuous sawtooth script, used for the first live captures. `ContinuousSawtoothBackend` is derived from this. Can be run directly as a fallback if `radar_mode_a.py` has issues. | Yes |
| `recordings/` | All saved sessions. Each subfolder contains `config.json`, `labels.json`, `raw_bursts.npy`, `spectrograms.npy`, `config_adi.npy`. | No |

---

## 2. Hardware Chain and What Can Go Wrong

```
WSL (Python)
  └── IIO network context
        └── phaser.local:50901  (iiod running on the Pi, forwarding Pluto)
              └── CN0566 Phaser  (ADF4159 PLL + 2x ADAR1000 beamformer)
                    └── AD9361 Pluto  (SDR, TX + RX)
```

| Layer | Common failure | Symptom |
|---|---|---|
| WSL network | `phaser.local` not resolving | `iio_attr` or `adi.ad9361()` throws immediately |
| iiod on Pi | Service not running or wrong port | `Connection refused` on `50901` |
| Pluto firmware | < 0.39 for TDD path | TDD backend connect fails; continuous still works |
| IIO timeout | Large buffer over slow link | `rx()` hangs; set `_ctx.set_timeout(0)` (already done in backends) |
| PLL not locking | Wrong VCO freq calculation | Flat spectrum, no IF peak visible in waterfall |
| TX cyclic buffer | Stale buffer from previous crash | Clear with `my_sdr.tx_destroy_buffer()` before connect |

---

## 3. Pre-Flight Checks

Run these before every session to confirm the chain is alive.

```bash
# 1. Ping the Pi
ping phaser.local

# 2. Confirm iiod is reachable and Pluto is visible
iio_info -u ip:phaser.local:50901 | head -20

# 3. Check adi version (needs to be recent for TDD)
python -c "import adi; print(adi.__version__)"

# 4. Confirm DSP imports cleanly (no hardware needed)
python -c "from radar_dsp import RadarConfig, process_frame; print('DSP OK')"
```

---

## 4. Stage 0 — Waterfall Sanity Baseline

**Script:** `RADAR_FFT_Waterfall_baseline.py`  
**Purpose:** Verify hardware bring-up, PLL lock, and IF return signal before running any SENTINEL code.

```bash
python RADAR_FFT_Waterfall_baseline.py
```

**What you should see:**  
A PyQtGraph window with two panels: FFT spectrum (top) and waterfall (bottom). There should be a peak near 100 kHz in the FFT panel. Move your hand in front of the Phaser — the peak should shift frequency slightly.

**What success means:**  
- PLL is locked
- TX tone is transmitting
- RX is receiving a return
- The WSL → Pi → Pluto IIO chain is working

**If this fails, do not proceed to Stages 2 or 3.** Fix the hardware chain first.

Close the window before running anything else — only one process can hold the IIO context at a time.

---

## 5. Stage 1 — Offline Replay

**Script:** `offline_replay.py`  
**Purpose:** Replay a saved session through the DSP stack without hardware. Use this to verify DSP logic, check a recorded dataset, or tune display parameters.

```bash
# Replay a session directory
python offline_replay.py recordings/20260403_124003

# Replay a raw ADI-format capture
python offline_replay.py notdd_4MSPS_500M_500u_64.npy

# Replay headless (no display), write frames to disk
python offline_replay.py recordings/20260403_124003 --no-plot --out processed/

# Replay with MTI off
python offline_replay.py recordings/20260403_124003 --no-mti

# Replay only first 50 frames
python offline_replay.py recordings/20260403_124003 --frames 50
```

See [Section 9](#9-all-cli-flags--offline_replaypy) for all flags.

---

## 6. Stage 2 — Live Mode A (Continuous Backend)

**Start here for all live sessions.** The continuous backend is safe over the WSL → Pi → Pluto network path. No TDD timing races, no burst GPIO, no Pluto firmware version dependency.

```bash
python radar_mode_a.py --acq continuous
```

A two-panel matplotlib window opens:
- **Left panel:** live range-Doppler map. Range increases upward. Velocity increases rightward. The cyan dashed line marks the auto-selected range gate.
- **Right panel:** rolling micro-Doppler history. Each column is one frame. The y-axis is velocity. Patterns here reveal micro-motion signatures over time.

Wave your hand in front of the antenna. You should see a moving smear appear in both panels within 1-2 seconds.

Press `CTRL+C` in the terminal to stop. If `--no-save` is not set, the session is saved automatically on exit.

---

## 7. Stage 3 — Live Mode A (TDD Backend)

> **WSL → Pi → Pluto forwarded path: TDD does not work here.**
> Both `--acq tdd` and `--acq tdd --no-tdd-ext-sync` will connect and open the
> display, but the first `capture()` call will fail with `TimeoutError: [Errno 110]
> Connection timed out`. This is not a code bug.

**Why it fails:** TDD `single_sawtooth_burst` mode requires the Pluto to fill one
large DMA buffer (262,144 samples ≈ 2 MB) in a single burst. The Pi's `iiod`
process cannot sustain this transfer over the forwarded TCP context and drops the
connection. The `set_timeout(0)` call suppresses the IIO-layer timeout, but the
OS-level TCP connection reset (`ETIMEDOUT`) cannot be overridden.

**Why continuous works:** The continuous sawtooth backend uses a smaller buffer
and the ADC fills it in a steady stream — no single large DMA burst.

**Reference note:** `reference/Range_Doppler_Plot.py` was written to run **on the
Pi**, with `sdr_ip = "ip:192.168.2.1"` (direct Pluto USB/network) and the script
executed locally. There is no network hop between the script and the Pluto in that
configuration.

### To use TDD — option A: SSH into the Pi and run there

```bash
ssh pi@phaser.local
cd /path/to/phaser_perso
python radar_mode_a.py --acq tdd \
    --rpi-uri ip:phaser.local \
    --sdr-uri ip:192.168.2.1
```

The Pluto is connected to the Pi via USB. `192.168.2.1` is the Pluto's default
USB-network address. The `rpi_uri` still reaches the Phaser over the Pi's own
localhost.

### To use TDD — option B: direct Pluto connection from WSL

Only works if your WSL host can reach `192.168.2.1` directly (USB passthrough via
`usbipd` or a direct Ethernet connection to the Pluto).

```bash
python radar_mode_a.py --acq tdd \
    --sdr-uri ip:192.168.2.1 \
    --rpi-uri ip:phaser.local
```

### Verify direct Pluto reachability first

```bash
iio_info -u ip:192.168.2.1 | head -5
```

If that returns device info, option B is available. If it hangs or errors, you need
option A (SSH on Pi).

### In the meantime: continuous is equivalent for Mode A data collection

The DSP pipeline is identical between both backends. Sessions recorded with
`--acq continuous` are fully replayable and suitable for all micro-Doppler
analysis and dataset generation work.

---

## 8. All CLI Flags — `radar_mode_a.py`

### Acquisition

| Flag | Default | Description |
|---|---|---|
| `--acq {tdd,continuous}` | `continuous` | Which backend to use. Always start with `continuous`. |
| `--frames N` | None (unlimited) | Stop after N frames. Omit to run until CTRL+C. |

### Radar Tuning

These map directly to ADF4159 PLL registers and AD9361 SDR settings. Change one at a time and verify before proceeding.

| Flag | Default | Description |
|---|---|---|
| `--bw Hz` | `500e6` | Chirp bandwidth in Hz. Determines range resolution: `R_res = c / (2 * BW)`. 500 MHz → 0.30 m, 200 MHz → 0.75 m. |
| `--ramp-time-us us` | `500` | Ramp duration in microseconds. Determines samples per chirp at 4 MHz: 500 us → 2000 samples. Longer ramps → more range bins. |
| `--num-chirps N` | `64` | Chirps per frame. Determines Doppler resolution: `v_res = λ / (2 * N * PRI)`. More chirps → finer Doppler, longer frame time. |
| `--sample-rate Hz` | `4e6` | AD9361 ADC sample rate. 4 MHz is stable over the network. Do not increase without testing. |
| `--rx-gain dB` | `60` | Receive gain, -3 to 70 dB. 60 is good for indoor. Reduce if ADC saturates (visible as smeared bands across all ranges). |
| `--tx-gain dB` | `0` | Transmit gain, 0 to -88 dB. 0 = max power. Reduce if TX leakage saturates the near-range bins. |
| `--output-freq Hz` | `9.9e9` | RF output frequency. Must match the physical antenna tuning. Do not change unless you know what you are doing. |
| `--center-freq Hz` | `2.1e9` | SDR LO frequency. Together with `--output-freq` and `--signal-freq` determines the VCO programming. |
| `--signal-freq Hz` | `100e3` | IF tone frequency. The TX sinewave frequency. Sets where the beat signal appears in the RX spectrum. |
| `--gain-taper g0,..,g7` | `127,127,...` | 8 ADAR1000 element gains (0-127). Uniform `127` = max gain, no taper. Blackman taper = `8,34,84,127,127,84,34,8` for lower sidelobes. |
| `--steer DEG` | `0.0` | Beam steering angle in degrees. Programs a phase gradient across the array elements. Positive = right, negative = left. |

### DSP

| Flag | Default | Description |
|---|---|---|
| `--mti` / `--no-mti` | on | 2-pulse MTI filter. Cancels stationary clutter by subtracting phase-aligned successive chirps. Leave on unless you want to see clutter to diagnose a problem. |
| `--range-bin N` | None (auto) | Fix the range gate to bin N. The micro-Doppler history and gate line will stay locked to this bin. Useful when tracking a known target at a fixed range. |
| `--min-range-bin N` | `5` | Exclude bins below N from auto range gate selection. Prevents the auto gate from locking onto TX-RX leakage at near-zero range. Increase if the gate keeps jumping to bin 0. |
| `--history-len N` | `64` | Depth of the micro-Doppler rolling history in frames. Increase for longer motion signatures. Decrease for faster display response. |
| `--min-scale F` | `0.0` | Log10 colormap floor. Values below this are clipped to the minimum colour. Increase to reduce noise floor visibility. |
| `--max-scale F` | `8.0` | Log10 colormap ceiling. Values above this are clipped to the maximum colour. Decrease to reveal weaker targets against a bright background. |
| `--max-range m` | `100.0` | Display range axis upper limit. Does not affect processing, only the Y axis scale on the plot. |
| `--max-vel m/s` | `10.0` | Display velocity axis half-width. Does not affect processing, only the X axis scale. |

### Display

| Flag | Default | Description |
|---|---|---|
| `--no-plot` | off | Disable the matplotlib GUI. Use this for headless operation, automated capture, or SSH sessions without X forwarding. The terminal will still print frame progress every 20 frames. |

### Recording

| Flag | Default | Description |
|---|---|---|
| `--save-dir DIR` | `recordings/` | Base directory. Each session creates a timestamped subdirectory inside. |
| `--no-save` | off | Disable all recording. Nothing is written to disk. Saves memory on long headless runs. |
| `--session-name NAME` | timestamp | Override the session subdirectory name. Useful for labelling captures by scenario. |

### Hardware URIs

| Flag | Default | Description |
|---|---|---|
| `--rpi-uri URI` | `ip:phaser.local` | IIO URI for the Raspberry Pi (hosts the Phaser CN0566 and runs iiod). |
| `--sdr-uri URI` | `ip:phaser.local:50901` | IIO URI for the Pluto SDR via context forwarding on the Pi. If using direct USB connection: `ip:192.168.2.1`. |

### TDD-only Options

These have no effect when `--acq continuous` is used.

| Flag | Default | Description |
|---|---|---|
| `--frame-guard-ms ms` | `0.2` | Guard time in milliseconds added to the ramp duration to form the PRI. PRI = ramp_time_us/1000 + frame_guard_ms. Matches `Range_Doppler_Plot.py` default of 0.2 ms. |
| `--no-tdd-ext-sync` | off (ext sync on) | Use internal TDD trigger instead of external GPIO sync. Try this if `--acq tdd` connects but capture returns all zeros or times out. |

---

## 9. All CLI Flags — `offline_replay.py`

```bash
python offline_replay.py SOURCE [options]
```

`SOURCE` is either a session directory path or a `.npy` file path.

| Flag | Default | Description |
|---|---|---|
| `--mti` / `--no-mti` | on | Apply or skip the 2-pulse MTI filter during replay. Disable to see raw clutter floor. |
| `--cfar` | off | Enable CFAR detection on the range profile during replay. Prints detection events to the terminal. |
| `--cfar-guard N` | `15` | CFAR guard cells (bins excluded around the cell under test). |
| `--cfar-ref N` | `16` | CFAR reference cells (bins used to estimate the noise floor on each side). |
| `--cfar-bias dB` | `25.0` | CFAR threshold bias above noise floor. Lower = more detections (more false alarms). Higher = fewer detections (more misses). |
| `--range-bin N` | None (auto) | Fix the range gate for the micro-Doppler slice shown in the terminal log. |
| `--min-range-bin N` | `5` | Minimum bin for auto range gate. |
| `--min-scale F` | `0.0` | Colormap floor (log10). |
| `--max-scale F` | `8.0` | Colormap ceiling (log10). |
| `--max-range m` | `100.0` | Plot Y axis upper limit. |
| `--frames N` | None (all) | Replay only the first N frames. |
| `--no-plot` | off | Skip the display. Useful when writing frames to disk with `--out`. |
| `--out DIR` | None | Write each processed range-Doppler frame as `frame_NNNNN.npy` into DIR. Enables offline batch processing and ML pipeline input. |
| `--pause s` | `0.05` | Seconds between frames during live display. Reduce for faster replay, increase to examine individual frames. |

---

## 10. The Display — What Everything Means

### Left panel: Range-Doppler map

```
Y axis = range (metres, 0 at bottom)
X axis = velocity (m/s, 0 at centre, positive = approaching)
Colour = log10 magnitude (inferno: black=low, yellow=high)
Cyan dashed line = auto-selected range gate
```

| What you see | What it means |
|---|---|
| Bright horizontal band at range 0 | TX-RX direct leakage. Always present. Raise `--min-range-bin` to skip it in auto gate. |
| Bright vertical band at velocity 0 | Static clutter. MTI should suppress this to noise floor. If it persists, MTI is off or the target is not moving. |
| Bright smear at a specific range and non-zero velocity | Moving target at that range. |
| Uniform brightness across all bins | ADC saturation (lower `--rx-gain`) or buffer sizing problem (check valid_chirps in terminal). |
| Horizontal stripes repeating every N bins | Aliasing or inter-chirp phase wrap. Lower `--bw` or increase `--ramp-time-us`. |
| Map is all minimum colour | No signal. PLL not locked, TX not transmitting, or all chirps invalid. Check Stage 0 first. |

### Right panel: Micro-Doppler history

```
Y axis = velocity (m/s)
X axis = frame index (time flows left to right, newest on right)
Colour = log10 magnitude (viridis: purple=low, yellow=high)
```

Each column is one processed frame's Doppler slice extracted at the range gate. Over time this builds a spectrogram of micro-motion at the target range.

| Pattern | Meaning |
|---|---|
| Horizontal bright band at 0 m/s | Clutter not fully cancelled (or MTI off) |
| Diagonal stripe | Accelerating or decelerating target |
| Oscillating wavy band | Periodic micro-motion (e.g. breathing, limb swing) |
| Symmetric harmonics above and below 0 | Walking gait signature |
| Solid block of colour | Target filling the range gate for multiple frames |

### Terminal output (every 20 frames)

```
  frame  120  range_bin= 402  valid_chirps=64/64
```

- `range_bin` — which range bin the gate is locked to. Stable value = stable target.
- `valid_chirps=64/64` — all chirps in the matrix were fully populated. If this is less than the total (e.g. `60/64`), some chirps were zero-padded because the buffer ran short. This is normal for the very first frame of a continuous capture and should not persist.

---

## 11. Tuning Ladder

Work through these in order. Verify stability (10+ consecutive frames with `valid_chirps=N/N` and a visible target response) before moving to the next step.

| Step | Command | What changes |
|---|---|---|
| 1 | `python radar_mode_a.py --acq continuous` | Baseline: 500 MHz BW, 500 us ramp, 64 chirps. Range res = 0.30 m, Doppler res = 0.47 m/s. |
| 2 | `... --num-chirps 128` | Double Doppler resolution (0.24 m/s). Frame time doubles to ~64 ms. |
| 3 | `... --bw 200e6 --num-chirps 128` | Coarser range (0.75 m) but cleaner chirp linearity at lower BW. |
| 4 | `... --bw 200e6 --ramp-time-us 200 --num-chirps 128` | Shorter ramp → faster frames (25.6 ms). Fewer samples per chirp (800). |
| 5 | `... --bw 200e6 --ramp-time-us 200 --num-chirps 256` | Best Doppler resolution. Only attempt after step 4 is stable. |

For **TDD**, run each of these with `--acq tdd` after confirming the continuous version works.

---

## 12. Recording and Replay Workflow

### Save a session

```bash
python radar_mode_a.py --acq continuous --session-name walk_test_1
# ... collect data, press CTRL+C ...
# Recording saved: recordings/walk_test_1/
```

### What gets saved

```
recordings/walk_test_1/
    config.json          -- full RadarConfig as JSON (human-readable)
    labels.json          -- placeholder label segments (edit manually to annotate)
    raw_bursts.npy       -- (N_frames, num_chirps, spc) complex64
    spectrograms.npy     -- (N_frames, num_chirps) float32 micro-Doppler slices
    config_adi.npy       -- 7-element array [sample_rate, signal_freq, output_freq,
                            num_chirps, chirp_BW, ramp_time_s, frame_length_ms]
                            compatible with reference/Range_Doppler_Processing.py
```

### Replay a saved session

```bash
python offline_replay.py recordings/walk_test_1
```

### Reprocess with different DSP settings

```bash
# Replay with MTI off and adjusted colour scale
python offline_replay.py recordings/walk_test_1 --no-mti --min-scale 3 --max-scale 7

# Write processed frames to disk for ML pipeline
python offline_replay.py recordings/walk_test_1 --no-plot --out processed/walk_test_1/
```

### Load in Python directly

```python
from radar_dsp import load_capture, process_frame, RadarConfig

bursts, cfg = load_capture("recordings/walk_test_1")
# bursts.shape == (N_frames, num_chirps, spc)
print(cfg.range_res_m, cfg.vel_res_m_s)

for frame in bursts:
    result = process_frame(frame, apply_mti_filter=True)
    rd_map = result["rd_map"]          # (spc, num_chirps) float32
    range_bin = result["range_bin"]    # int
    micro_doppler = result["micro_doppler"]  # (num_chirps,) float32
```

### Use the ADI offline processing script directly

The saved `config_adi.npy` is compatible with `reference/Range_Doppler_Processing.py`. To use it:

```python
# In Range_Doppler_Processing.py, point f to the raw_bursts file
# and load config_adi.npy instead of the default _config.npy
f = "recordings/walk_test_1/raw_bursts.npy"
config = np.load("recordings/walk_test_1/config_adi.npy")
```

---

## 13. DSP Reference — What Each Stage Does

All stages are in `radar_dsp.py`. This is what happens inside `process_frame()`.

### 1. Chirp matrix input

Shape: `(num_chirps, samples_per_chirp)`, dtype `complex64`.  
Row k = the IF beat signal from the k-th chirp.  
Columns = fast-time (range). Rows = slow-time (Doppler).

### 2. 2-pulse MTI filter (`apply_mti`)

```
for each chirp k:
    phi = angle(correlate(chirp_k, chirp_{k+1}))
    filtered[k] = chirp_{k+1} - chirp_k * exp(-j*phi)
```

The phase correlation compensates for slow inter-chirp phase drift (common on the network path). The subtraction cancels signals with the same phase between pulses — i.e. stationary targets and direct leakage. Moving targets survive because their phase advances between chirps.

**When to disable:** If you want to see clutter to diagnose a hardware problem. If you are collecting training data and want the network to learn clutter suppression itself.

### 3. 2D FFT (`range_doppler_map`)

```
fftshift(abs(fft2(chirp_matrix))).T
```

- `fft2` applies a range FFT along each chirp row (fast-time → range bins) and a Doppler FFT across all chirps (slow-time → velocity bins).
- `fftshift` centres zero frequency (zero range, zero velocity) in the middle.
- `.T` transposes so the output is `(n_range, n_doppler)` — range on axis 0, Doppler on axis 1.
- `log10(result + 1e-10)` converts to log scale for display.

### 4. Range profile (`range_profile`)

`max(rd_map, axis=1)` — collapse the Doppler axis by taking the maximum across all velocity bins for each range bin. The result is a 1D profile of the strongest return at each range.

### 5. Range gate selection (`select_range_gate`)

Auto: `argmax(range_profile[min_range_bin:])`. Returns the range bin with the strongest return, ignoring bins below `min_range_bin` (near-field leakage).  
Manual: clamp the specified `--range-bin` to valid index range.

### 6. Micro-Doppler slice

`rd_map[range_bin, :]` — the full Doppler spectrum at the selected range gate. This 1D vector is appended to the rolling history each frame.

---

## 14. Troubleshooting

### Hardware fails to connect

```
RuntimeError: iio_create_network_context failed
```

- Check `ping phaser.local` resolves.
- Check `iio_info -u ip:phaser.local:50901` lists Pluto devices.
- Only one IIO context can be open at a time. Kill any other Python processes that might be holding it.

```bash
pkill -f "python.*radar"
pkill -f "python.*Range_Doppler"
```

### TX buffer error on startup

```
Exception: TX buffer not created
```

The Pluto TX buffer was not cleanly destroyed by a previous session (crash, CTRL+C during connect). The backend already calls `tx_destroy_buffer()` before creating a new one, but if it fails:

```bash
# Connect manually with pyadi-iio and clear the buffer
python -c "import adi; s=adi.ad9361('ip:phaser.local:50901'); s.tx_destroy_buffer(); print('cleared')"
```

### Range-Doppler map is all zeros or very faint

1. Check the Stage 0 waterfall — if that is also flat, the PLL is not locked.
2. Confirm `--output-freq` matches the hardware tuning. The default `9.9e9` is for the standard CN0566 IF cable configuration.
3. Try `--rx-gain 70` to maximise receive sensitivity.
4. Try `--no-mti` — if signal appears, the target is stationary and MTI is correctly cancelling it.

### valid_chirps is consistently low (e.g. 48/64)

The buffer is not large enough to hold all chirps. This can happen if `ramp_time_us` was changed after the buffer was sized. The backend sizes the buffer on `connect()` from the programmed ramp time read back from the hardware. If `freq_dev_time` was rounded by the hardware to a different value:

```bash
# Add a few extra chirps of headroom via a longer ramp
python radar_mode_a.py --acq continuous --ramp-time-us 520
```

### TDD capture times out (`TimeoutError: [Errno 110] Connection timed out`)

This is the expected failure mode when running `--acq tdd` over the
`phaser.local:50901` forwarded IIO context. It is a transport-layer issue,
not a code bug. See [Section 7](#7-stage-3--live-mode-a-tdd-backend) for the
full explanation and both fix options.

Short version: either SSH into the Pi and run with `--sdr-uri ip:192.168.2.1`,
or switch to `--acq continuous`.

### TDD capture returns all-zero chirps (if TDD does connect)

The TDD burst trigger GPIO (`gpio_burst`) pulse fired before the RX buffer was
ready, so the burst was missed. Only relevant when running on the Pi directly.

1. Try `--no-tdd-ext-sync` (internal TDD trigger, no external GPIO dependency).
2. If still wrong, fall back to `--acq continuous`.

### Display is blank or not updating

Matplotlib in non-interactive mode will not update if the backend does not support `draw_idle()`. On WSL without X forwarding or with a headless backend:

```bash
MPLBACKEND=TkAgg python radar_mode_a.py --acq continuous
# or
MPLBACKEND=Qt5Agg python radar_mode_a.py --acq continuous
# or run headless and check the terminal printout instead
python radar_mode_a.py --acq continuous --no-plot
```

### Replay crashes on legacy session directory

```
KeyError: 'chirp_bw'
```

The session was saved by an older script using `chirp_BW` (uppercase). `radar_dsp.load_capture()` handles this via `RadarConfig.from_dict()` which accepts both key names. If you are using `sentinel_core.load_adi_capture()` directly, use `load_capture` from `radar_dsp` instead.


Try this reset sequence on the Pi:

sudo systemctl restart iiod-usb@001.003.5
sudo systemctl restart iiod
Then on the WSL machine, immediately rerun:

python3 /mnt/e/work/agent/workspace/radar/phaser_perso/RADAR_FFT_Waterfall_baseline.py
If it still freezes, do the hard reset in this order:

Stop both services:
sudo systemctl stop iiod-usb@001.003.5
sudo systemctl stop iiod
Power-cycle the Pluto (unplug/replug USB into the Pi).

Start services:

sudo systemctl start iiod-usb@001.003.5
sudo systemctl start iiod
Run the baseline again.
If you want a more surgical check, run this on WSL before launching the GUI:

python3 - <<'PY'
import adi
sdr = adi.ad9361(uri="ip:phaser.local:50901")
sdr.rx_buffer_size = 4096
print("rx() begin")
print(sdr.rx()[0][:5])
print("rx() done")
PY
If that hangs, the transport is stuck even without Qt.