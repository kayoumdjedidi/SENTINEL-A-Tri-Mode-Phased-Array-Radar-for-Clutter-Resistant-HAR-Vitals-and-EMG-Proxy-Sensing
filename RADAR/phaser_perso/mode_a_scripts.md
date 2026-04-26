# SENTINEL Mode A Script Map

Mode A is the FMCW range-Doppler / micro-Doppler mode. This note documents the current baseline and the older scripts that were built while debugging acquisition, TDD, transport, and DSP artifacts.

## Current Decision

Use native Windows Python for live hardware work.

Current preferred entry point:

```powershell
python .\sentinel_mode_a_v5.py
```

Current proven topology:

```text
Windows Python -> Pluto: ip:192.168.2.1 for TDD soft-sync live acquisition
Windows Python -> Pluto: usb:1.5.5 also works for direct continuous RX
Windows Python -> Phaser/CN0566: ip:phaser.local
Do not use: ip:phaser.local:50901 for live RX
Do not use: WSL for live Pluto RX
```

Current acquisition status:

- `--acq tdd --tdd-trigger soft` is the preferred reference-aligned path.
- Continuous acquisition is a validated fallback.
- External `gpio_burst` TDD is not supported by the current exposed Pluto GPIO/device-tree path.
- The older WSL/Pi-forwarded IIO path repeatedly failed with `ETIMEDOUT` / `[Errno 110] host unreachable` during `s.rx()`.

## Quick Run Commands

Production-style v5 live run:

```powershell
python .\sentinel_mode_a_v5.py
```

v5 with lighter CPI for faster iteration:

```powershell
python .\sentinel_mode_a_v5.py --num-chirps 128
```

Known-good TDD soft-sync command from the debugging phase:

```powershell
python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 0 --save-dir recordings --session-name win_tdd_soft_live
```

Continuous fallback:

```powershell
python .\radar_mode_a.py --acq continuous --sdr-uri usb:1.5.5 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 500 --num-chirps 64 --frames 0 --max-range 10 --save-dir recordings --session-name win_cont_live_10m
```

Replay a saved session:

```powershell
python .\offline_replay.py recordings\win_tdd_soft_live --frames 50 --max-range 10
```

## Script Status Table

| Script | Status | Purpose | Use Now? |
|---|---|---|---|
| `sentinel_mode_a_v5.py` | Current baseline | Windows-native Mode A GUI, TDD soft-sync defaults, PyQt5/pyqtgraph display, improved range-gating defaults. | Yes, default live script. |
| `radar_mode_a.py` | Proven diagnostic/runtime | Matplotlib Mode A runner with `continuous` and `tdd` backends. This is where TDD soft-sync was proven first. | Yes for controlled captures, debugging, and fallback. |
| `radar_backends.py` | Active support module | Hardware bring-up and acquisition backends used by `radar_mode_a.py` and `sentinel_mode_a_v5.py`. Contains continuous and TDD paths. | Yes, but not run directly. |
| `radar_dsp.py` | Active support module | Backend-neutral FMCW DSP: range-Doppler FFT, MTI, axes, micro-Doppler history, session save/load. | Yes, import only. |
| `target_detection_dbfs.py` | Active support module | CFAR-style detection helper used by the Mode A GUI layers. | Yes, import only. |
| `offline_replay.py` | Active replay tool | Replays saved `radar_mode_a.py` / `radar_dsp.py` sessions without hardware. | Yes. |
| `pluto_rx_sweep.py` | Active diagnostic | Raw Pluto data-plane validation over `usb:*` and `ip:*`, channel count, sample rate, and buffer size. | Yes when RX fails. |
| `pluto_tdd_probe.py` | Active diagnostic | Isolates Pluto TDD trigger modes: `soft`, `internal`, `external`; dumps TDD/GPIO attributes. | Yes when TDD fails. |
| `sentinel_mode_a_claude_handoff.md` | Active handoff | Debug history, proven commands, avoid-list, TDD status. | Yes as context for agents. |

## Historical Runtime Scripts

| Script | What It Tried | Outcome / Lesson |
|---|---|---|
| `RADAR_FFT_Waterfall_baseline.py` | First FMCW waterfall sanity check, close to ADI continuous-triangular reference. | Useful for checking that Phaser/PLL/TX/RX are alive, but not the final Mode A pipeline. |
| `RADAR_FFT_Waterfall_fullband.py` | Full-band variant of the waterfall sanity check. | Useful for broad hardware visibility; not final Mode A. |
| `Range_Doppler_NoTDD.py` | Continuous sawtooth range-Doppler without TDD. | Proved that useful Mode A acquisition could run without burst sync. Became the ancestor of the continuous backend. |
| `sentinel_radar.py` | Early production PyQt Mode A script using competition-spec defaults. | Good GUI foundation, but old topology/defaults assumed forwarded IIO and max RX gain. Superseded. |
| `sentinel_mode_a.py` | Reference-first wrapper with TDD and continuous options. | Transitional script; kept for reference, not the main path. |
| `sentinel_modeA_v2.py` | Indoor-optimized PyQt Mode A with labels and manual range gate. | Useful UI/labeling ideas; old defaults/topology. Superseded. |
| `sentinel_modeA_v3.py` | Tried non-10-us ramp timing to move leakage away from DC. | Important DSP experiment, but not the final acquisition path. |
| `sentinel_modeA_v4.py` | Tried chirp deskew, empirical leakage detection, and linear background subtraction. | Useful artifact-reduction ideas, but v5 is the current integration point. |
| `sentinel_modeA_ref_tdd.py` | Near-verbatim ADI TDD reference path with split URI and DMA-first experiments. | Helped localize TDD trigger issues. Do not use as the default live path; `sync_soft` in `radar_mode_a.py` solved the practical TDD path. |
| `sentinel_modeA_ref_contsync.py` | Reference-inspired continuous mode with per-frame chirp boundary search. | Useful experiment for continuous chirp alignment; not the main runtime. |
| `sentinel_fmcw_baseline.py` | Thin wrapper around `RADAR_FFT_Waterfall_baseline.py`. | Convenience launcher only. |

## Newer Parallel/Offline Core

These files represent a smaller alternate core used by some earlier runners. They are useful for reference but are not the main v5 stack.

| Script | Role |
|---|---|
| `sentinel_core.py` | Compact RadarConfig, axes, segmentation, save/load, and offline processing utilities. |
| `sentinel_backends.py` | Earlier backend abstraction paired with `sentinel_core.py`. Some defaults still reflect the old forwarded-IIO topology. |
| `sentinel_rd_offline.py` | Offline replay tool for captures in the `sentinel_core.py` format. |

Use `radar_dsp.py` / `radar_backends.py` / `sentinel_mode_a_v5.py` for the current Mode A path unless deliberately testing this alternate core.

## ADI Reference Scripts

Reference scripts live under `reference/`. Treat them as source material, not day-to-day runtime scripts.

| Script | Purpose |
|---|---|
| `reference/FMCW_RADAR_Waterfall.py` | ADI-style continuous FMCW waterfall. Useful for sanity comparison. |
| `reference/FMCW_RADAR_Waterfall_ChirpSync.py` | ADI-style chirp-sync/TDD waterfall reference. |
| `reference/Range_Doppler_Plot.py` | Golden ADI TDD range-Doppler script. Original reference for burst slicing and TDD setup. |
| `reference/Range_Doppler_Processing.py` | Offline processor for ADI-format saved captures. |
| `reference/CFAR_RADAR_Waterfall.py` and `reference/CFAR_RADAR_Waterfall_ChirpSync.py` | ADI CFAR demo variants. |

## Debugging Timeline

1. Waterfall scripts proved the Phaser/Pluto RF chain could produce an IF return.
2. `Range_Doppler_NoTDD.py` proved continuous sawtooth could produce range-Doppler without TDD.
3. `radar_mode_a.py` unified continuous and TDD backends and added saving/replay.
4. `sentinel_radar.py` and `sentinel_modeA_v2.py` moved toward a PyQt GUI and competition-style controls.
5. `sentinel_modeA_v3.py` and `sentinel_modeA_v4.py` explored leakage/DC/artifact fixes.
6. Native Windows direct Pluto tests proved high-rate RX works over both `usb:1.5.5` and `ip:192.168.2.1`.
7. TDD initially failed with internal/external trigger assumptions.
8. `pluto_tdd_probe.py` exposed `sync_soft` as the missing trigger path.
9. `radar_mode_a.py --tdd-trigger soft` validated TDD captures and live runs.
10. `sentinel_mode_a_v5.py` consolidated the working topology, TDD soft-sync defaults, and PyQt display.

## Known-Good Evidence

Validated raw Pluto RX:

- `usb:1.5.5`
- `ip:192.168.2.1`
- `1 MSPS` and `4 MSPS`
- one-channel and two-channel receive
- buffers through `524288`

Validated Mode A captures:

- `recordings\win_cont_usb_small`
- `recordings\win_tdd_soft_64`
- `recordings\win_tdd_soft_128`
- `recordings\win_tdd_soft_live`

Validated TDD soft-sync behavior:

- `--tdd-trigger soft` fills the DMA buffer.
- Saved TDD soft-sync captures have finite spectrograms.
- Live TDD reported `valid_chirps=64/64` during the validated run.

## Avoid List

Do not use these as default runtime paths:

```text
ip:phaser.local:50901
ip:127.0.0.1:50901
WSL live RX
Pi forwarded iiod live RX
Pi-side usb:* when Pluto USB is physically attached to the PC
external gpio_burst TDD as a software-only task
```

Avoid these scripts for final live results unless you are intentionally reproducing an old experiment:

```text
sentinel_radar.py
sentinel_mode_a.py
sentinel_modeA_v2.py
sentinel_modeA_v3.py
sentinel_modeA_v4.py
sentinel_modeA_ref_tdd.py
Range_Doppler_NoTDD.py
RADAR_FFT_Waterfall_baseline.py
RADAR_FFT_Waterfall_fullband.py
```

## When To Use Which Script

| Task | Use |
|---|---|
| Normal live Mode A demo/data collection | `sentinel_mode_a_v5.py` |
| Reproduce validated TDD soft-sync capture commands | `radar_mode_a.py --acq tdd --tdd-trigger soft` |
| Continuous fallback capture | `radar_mode_a.py --acq continuous` |
| Check Pluto RX health | `pluto_rx_sweep.py` |
| Check TDD trigger modes | `pluto_tdd_probe.py` |
| Replay saved Mode A captures | `offline_replay.py` |
| Quick RF waterfall sanity check | `RADAR_FFT_Waterfall_baseline.py` |
| Compare against ADI source behavior | `reference/*` scripts |

## Recommended Validation Protocol

Before a Mode A demo:

1. Activate the Windows radar venv.
2. Run `iio_info -s` and verify `ip:phaser.local`, `ip:192.168.2.1`, and preferably `usb:1.5.5` are visible.
3. If RX has been flaky, run `pluto_rx_sweep.py` before any radar script.
4. Run `sentinel_mode_a_v5.py` with the default TDD soft-sync path.
5. If TDD soft-sync fails, disable any stale TDD state and fall back to `radar_mode_a.py --acq continuous`.
6. Save a short recording and replay it with `offline_replay.py` to verify finite RD maps and expected target range.

## Interpretation Caveat

Mode A is the correct mode for physical range, Doppler, micro-Doppler, walking/gesture/fall/HAR features, and target tracking. It does not estimate heartbeat or EMG directly. Those belong to Mode B and Mode C.

If the display looks like diagonal striping, fixed-velocity clutter, or a far-range gate around tens of metres indoors, treat it as acquisition/DSP/display artifact first. Check min range, RX clipping, MTI, leakage bins, and replayed raw captures before interpreting the map physically.
