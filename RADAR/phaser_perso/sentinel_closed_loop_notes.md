# SENTINEL Closed-Loop Notes

This note documents the current closed-loop integration state after the HB100
and array-calibration fixes.

## Current Purpose

`sentinel_closed_loop.py` is the target-driven supervisor. It does not simply
cycle Mode A/B/C on a timer. The intended loop is:

1. Run Mode A FMCW continuously.
2. Detect candidate people/reflectors in range-Doppler.
3. Lock a target automatically or manually.
4. Interrogate that locked target with CW Modes B/C.
5. Return to Mode A and continue monitoring for movement, posture, or target changes.

Mode A is still the navigation/tracking mode. Modes B/C are short CW
interrogation windows for vitals and myo-index once a target has been selected.

## Calibration Files

There are two different calibration layers.

| File | Produced By | Meaning |
|---|---|---|
| `hb100_freq_val.pkl` | `hb100_calibrate.py` | External HB100 source frequency. |
| `channel_cal_val.pkl` | `phaser_array_calibrate.py` | RX channel compensation. |
| `gain_cal_val.pkl` | `phaser_array_calibrate.py` | CN0566 per-element gain correction. |
| `phase_cal_val.pkl` | `phaser_array_calibrate.py` | CN0566 per-element phase correction. |
| `phaser_array_cal_val.json` | `phaser_array_calibrate.py` | Human-readable array calibration metadata. |

Important: `hb100_freq_val.pkl` does not imply that `gain_cal_val.pkl` or
`phase_cal_val.pkl` exist. HB100 frequency calibration and Phaser array
gain/phase calibration are separate steps.

The loader in `radar_backends.py` now checks both:

- the current working directory
- the directory containing `radar_backends.py`

This avoids false missing-file reports when the app is launched from a different
directory.

## Calibration Workflow

Use reference topology for calibration:

```text
Pluto USB DATA -> Raspberry Pi
CN0566/Phaser  -> Raspberry Pi
Windows PC     -> network to phaser.local
HB100          -> powered and aimed at boresight
```

Step 1: measure the HB100 frequency:

```powershell
python .\hb100_calibrate.py --topology reference --print-contexts
```

Step 2: generate CN0566 array gain/phase calibration:

```powershell
python .\phaser_array_calibrate.py --topology reference --print-contexts
```

Step 3: move Pluto back to the normal SENTINEL topology:

```text
Pluto USB DATA -> Windows PC or direct Pluto network path
CN0566/Phaser  -> Raspberry Pi / ip:phaser.local
```

Step 4: run closed-loop SENTINEL:

```powershell
python .\sentinel_closed_loop.py --hb100-cal auto --cal-expected-range-m 1.0 --cal-target-max-m 3.0 --auto-interrogate
```

## Current Verified Calibration

The latest verified array calibration used:

```text
topology: reference-rpi
CN0566:   ip:phaser.local
Pluto:    ip:phaser.local:50901
HB100:    10.423193359 GHz
sample:   30 MSPS
rx_lo:    2.2 GHz
averages: 4
```

Saved files:

```text
channel_cal_val.pkl
gain_cal_val.pkl
phase_cal_val.pkl
phaser_array_cal_val.json
```

## Closed-Loop Behavior

`LOCKED` means Mode A has a target. It does not automatically mean Modes B/C are
running.

Modes B/C start in one of three ways:

- Run with `--auto-interrogate` and let the lock trigger CW automatically.
- Check `Auto CW interrogation` in the GUI after a target is locked.
- Press `Interrogate locked target` manually.

If Auto CW is off, the GUI status should say:

```text
manual mode: press 'Interrogate locked target' or enable Auto CW
```

This is intentional. It prevents the app from repeatedly tearing down Mode A and
opening CW while the target lock is still being tuned.

## CW-First Demo Mode

`sentinel_closed_loop_full.py` now defaults to CW-first demo mode:

```powershell
python .\sentinel_closed_loop_full.py
```

This does three things for recording:

- Skips the terminal room/target Mode A calibration by default.
- Builds empty Mode A plot axes without opening Mode A hardware first.
- Opens Modes B/C directly so the CW vitals and myo plots can populate first.

The equivalent explicit command is:

```powershell
python .\sentinel_closed_loop_full.py --start-mode cw
```

If you want the older Mode A-first behavior:

```powershell
python .\sentinel_closed_loop_full.py --start-mode fmcw
```

If you want CW-first but still want the terminal room/target calibration before
the GUI:

```powershell
python .\sentinel_closed_loop_full.py --start-mode cw --force-scene-calibration
```

For video stitching, the practical flow is:

1. Start in CW and record Mode B/C plots.
2. Press `Return to Mode A (FMCW)` and wait for the full close/open transition.
3. Record Mode A range-Doppler / scene-map footage.
4. Optionally press `Run Modes B + C (CW)` again for another CW segment.

This is intentionally not smooth. It prioritizes getting each mode visible and
recordable over maintaining a real-time seamless scheduler.

## Responsiveness Changes

Closed-loop Mode A now defaults to:

```text
num_chirps: 64
history_len: 48
```

This is deliberately lighter than the standalone Mode A tuning path. The
closed-loop script must leave enough headroom for GUI updates, lock logic, and
Mode B/C switching.

For higher-resolution Mode A-only testing, override explicitly:

```powershell
python .\sentinel_closed_loop.py --num-chirps 128
python .\sentinel_closed_loop.py --num-chirps 256
```

Use 128/256 only when Mode A quality matters more than fast closed-loop
response. For real closed-loop behavior, start with 64.

## Recent Code Changes

- Added `phaser_array_calibrate.py`.
- Documented the difference between HB100 frequency calibration and CN0566
  gain/phase array calibration.
- Made `radar_backends.py` print missing calibration messages once instead of
  repeatedly.
- Made calibration-file lookup robust to current-directory differences.
- Changed closed-loop default `--num-chirps` from 256 to 64.
- Changed closed-loop default `--history-len` from 64 to 48.
- Changed Auto CW so it can start Modes B/C from either manual locks or
  automatic locks.
- Added GUI status text explaining whether B/C is waiting for manual
  interrogation, auto cooldown, or target lock.
- Added `--start-mode {cw,fmcw}` to `sentinel_closed_loop_full.py`.
- Made `--start-mode cw` the default for demo capture, skipping terminal scene
  calibration unless `--force-scene-calibration` is provided.
- Added a CW-first boot path that opens Modes B/C without first opening Mode A.

## Practical Run Modes

Manual lock/debug mode:

```powershell
python .\sentinel_closed_loop.py --hb100-cal auto --cal-expected-range-m 1.0 --cal-target-max-m 3.0
```

Then click:

```text
Lock current target -> Interrogate locked target
```

Automatic B/C interrogation:

```powershell
python .\sentinel_closed_loop.py --hb100-cal auto --cal-expected-range-m 1.0 --cal-target-max-m 3.0 --auto-interrogate
```

Higher Mode A resolution, less responsive:

```powershell
python .\sentinel_closed_loop.py --num-chirps 128 --auto-interrogate
```

Fastest closed-loop debug:

```powershell
python .\sentinel_closed_loop.py --num-chirps 64 --cw-dwell-s 10 --cw-cooldown-s 5
```

CW-first recording demo:

```powershell
python .\sentinel_closed_loop_full.py
```

This defaults to `--start-mode cw --cw-source demo`. It opens the B/C plots from
a synthetic CW stream and does not touch Pluto hardware. Use this for recording
when the live hardware path is unstable. In this mode there should be no
`[sdr] using Pluto URI ...` or `[cw] TDD cleanup ...` messages; those indicate
that `--cw-source hardware` was passed or an older copy of the file is running.

The synthetic demo stream is not measured data. It is generated to exercise the
same Mode B/C DSP and GUI paths with plausible respiration, heartbeat, and
myo-index content.

Hardware CW, if needed:

```powershell
python .\sentinel_closed_loop_full.py --cw-source hardware --sdr-uri auto
```

In hardware mode, the app scans visible IIO contexts, tries visible Pluto
contexts first, then falls back to the known direct Pluto IPs.

Mode A-first recording demo:

```powershell
python .\sentinel_closed_loop_full.py --start-mode fmcw
```

If you want to inspect the visible Pluto context manually:

```powershell
iio_info -s
```

Then use whichever direct Pluto context is visible, usually one of:

```powershell
python .\sentinel_closed_loop_full.py --start-mode cw --sdr-uri usb:1.5.5
python .\sentinel_closed_loop_full.py --start-mode cw --sdr-uri ip:192.168.2.1
```

Do not use `ip:phaser.local:50901` for this CW-first demo unless the Pluto USB
data cable is still physically plugged into the Raspberry Pi. If the Pluto USB
data cable is plugged into Windows, `ip:phaser.local:50901` is the wrong path.

## Host-Unreachable Notes

`OSError: [Errno 110] host unreachable` during CW means the AD9361 context was
opened but the RX refill path did not deliver samples. The default CW demo path
does not open AD9361 at all, so it bypasses this failure class. For hardware CW,
the code now:

- disables stale TDD state before CW opens,
- destroys stale RX/TX buffers before starting the CW tone,
- validates three CW `rx()` calls before declaring Modes B/C running,
- retries CW open five times before giving up,
- keeps the GUI alive for short CW RX glitches instead of dying on the first one.

If it still happens immediately, it is almost always topology:

- Pluto plugged into Windows: use `--sdr-uri usb:1.5.5` or `--sdr-uri ip:192.168.2.1`.
- Pluto plugged into Raspberry Pi: use `--sdr-uri ip:phaser.local:50901`, but expect more latency.
- Any stale Python process holding Pluto: close all radar Python windows, then retry.
- TDD left wedged from a prior crash: run `python -c "import adi; t=adi.tddn('ip:192.168.2.1'); t.enable=False; print('TDD disabled')"` if Pluto is on Windows/direct IP.

## Known Limitations

- Switching between Mode A and CW can still stress the IIO path because it tears
  down and reopens SDR/Phaser contexts.
- If `ERROR: Open unlocked: -16` appears repeatedly, stop the app, ensure no
  other Python process owns Pluto/CN0566, disable stale TDD if needed, and power
  cycle only if the IIO context stays wedged.
- Mode B heartbeat is a mechanical chest-wall estimate, not ECG.
- Mode C myo-index is an RF amplitude-envelope proxy, not contact EMG.
